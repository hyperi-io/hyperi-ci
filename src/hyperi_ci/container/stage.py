# Project:   HyperI CI
# File:      src/hyperi_ci/container/stage.py
# Purpose:   Container build stage handler
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container build stage.

The image is always built from the repo's own Dockerfile.
``release.container.enabled`` gates it:

* ``auto`` (default): build when the Dockerfile exists. A library skips
  quietly; a runnable project with no Dockerfile skips with a warning.
* ``true``: build is required; no Dockerfile fails the stage.
* ``false``: explicit skip.

Push modes, resolved by :mod:`hyperi_ci.release_mode`:

* ``release``  -- release dispatch or Release-trailer push: full tag set.
* ``dev``      -- branch CI with the ``release.container.dev_push`` opt-in:
  ``branch-*`` / ``dev-sha-*`` tags only (:func:`resolve_tags`).
* ``validate`` -- push-to-main and local runs: build, no push.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from hyperi_ci.common import (
    ReleaseVersionError,
    error,
    group,
    holds_latest,
    info,
    is_github_actions,
    normalise_tristate,
    resolve_release_version,
    skip_optimize,
    success,
    warn,
)
from hyperi_ci.config import CIConfig, OrgConfig, load_org_config
from hyperi_ci.container.build import (
    BuildArgError,
    build_and_push,
    pushed_digest,
    render_build_args,
    resolve_tags,
    retry_settings,
)
from hyperi_ci.container.cgroup import builder_cgroup_parents, probe_cgroup_parent
from hyperi_ci.container.detect import detect
from hyperi_ci.container.labels import build_oci_labels
from hyperi_ci.container.registry import resolve_registry_bases
from hyperi_ci.release_branches import effective_release_channel
from hyperi_ci.release_mode import (
    DEV,
    RELEASE,
    VALIDATE,
    dev_branch_slug,
    resolve_push_mode,
)
from hyperi_ci.repo_path import RepoPathError, confine

# Languages whose Build stage ships the per-arch dist/ binaries the image copies.
_BINARY_LANGUAGES = {"rust", "golang"}

# `COPY --from=<stage> ... dist/` is excluded: that dist/ is built inside the
# Dockerfile and needs no CI artefacts.
_DIST_CONTEXT_COPY = re.compile(r"(?m)^\s*(?:COPY|ADD)\s+(?!--from[=\s])[^\n]*\bdist/")


def _read_version() -> str:
    """Return the image version: the release version, else the ref, else 0.0.0.

    The fallbacks exist because an image needs a concrete tag even when
    :func:`resolve_release_version` finds none.
    """
    return resolve_release_version() or os.environ.get(
        "GITHUB_REF_NAME", "0.0.0"
    ).removeprefix("v")


def _read_sha() -> str:
    long_sha = os.environ.get("GITHUB_SHA")
    if long_sha:
        return long_sha[:8]
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _section(config: CIConfig) -> dict:
    """Return ``release.container``, empty when a project set it to a scalar."""
    container_cfg = config.setting("release.container")
    return container_cfg if isinstance(container_cfg, dict) else {}


def _dev_push_opt_in(container_cfg: dict) -> bool:
    """Return the ``release.container.dev_push`` opt-in, coerced to bool."""
    raw = container_cfg.get("dev_push", False)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return bool(raw)


def should_build_container(config: CIConfig, *, language: str = "") -> tuple[bool, str]:
    """Return ``(build, reason)`` for :func:`run`'s gate, from the filesystem only.

    The workflow calls this before booting Buildx (issue #33), so a repo with
    no Dockerfile never pulls buildkit or logs in to GHCR. ``enabled: true``
    returns True even with no Dockerfile, and :func:`run` then fails.
    """
    enabled = normalise_tristate(
        config.setting("release.container.enabled"),
        key="release.container.enabled",
    )
    if enabled == "false":
        return False, "release.container.enabled: false"
    if enabled == "true":
        return True, "release.container.enabled: true"
    decision = detect(
        language=language,
        project_dir=Path.cwd(),
        dockerfile=config.setting("release.container.dockerfile"),
    )
    return decision.build, decision.reason


def _write_output(key: str, value: str) -> None:
    """Append ``key=value`` to ``$GITHUB_OUTPUT`` when it is set."""
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{key}={value}\n")


def _write_digest_outputs(version_tag: str, digest: str | None) -> None:
    """Expose the pushed image as ``digest`` and ``image`` (``<tag>@<digest>``)."""
    if digest is None:
        warn(f"buildx recorded no digest for {version_tag}; no image output is set")
        return
    info(f"Pushed image: {version_tag}@{digest}")
    _write_output("digest", digest)
    _write_output("image", f"{version_tag}@{digest}")


def _log_builder_cgroups() -> None:
    """Name the cgroup parent each buildx builder got, so a run log shows it."""
    if not is_github_actions():
        return
    builders = builder_cgroup_parents()
    if not builders:
        info("Buildx builder cgroup: no docker-container builder found")
    for name, parent in builders:
        info(f"Buildx builder cgroup: {name} under {parent or 'the daemon default'}")


def _confine_build_paths(config: CIConfig, project_dir: Path) -> bool:
    """Refuse a Dockerfile or build context outside the checkout.

    Either one hands docker a file the repo does not hold, on a runner whose
    ``~/.docker/config.json`` holds the registry logins.
    """
    try:
        for key in ("dockerfile", "context"):
            confine(
                str(config.setting(f"release.container.{key}")),
                project_dir,
                key=f"release.container.{key}",
            )
    except RepoPathError as exc:
        error(str(exc))
        return False
    return True


def run(config: CIConfig, *, language: str = "") -> int:
    """Run the container build stage.

    Args:
        config: Merged CI configuration.
        language: Detected project language.

    Returns:
        Exit code (0 = success or skipped).

    """
    project_dir = Path.cwd()
    # Checked in the resolve step too, so a refusal lands before any login.
    if not _confine_build_paths(config, project_dir):
        return 1

    # Ahead of resolve-only, which the workflow also sets so that a CLI release
    # without the probe resolves instead of building (issue #284).
    if os.environ.get("HYPERCI_CONTAINER_CGROUP_PROBE"):
        found = probe_cgroup_parent()
        if found.parent:
            info(f"Buildx cgroup parent: {found.parent} ({found.reason})")
        else:
            info(
                f"Buildx cgroup parent: not set, buildx keeps its default ({found.reason})"
            )
        _write_output("cgroup-parent", found.parent or "")
        return 0

    # The workflow gates Docker setup on this output, so it does no Docker work.
    if os.environ.get("HYPERCI_CONTAINER_RESOLVE_ONLY"):
        build, reason = should_build_container(config, language=language)
        info(f"Container resolve: build={'true' if build else 'false'} -- {reason}")
        _write_output("build", "true" if build else "false")
        return 0

    enabled = normalise_tristate(
        config.setting("release.container.enabled"),
        key="release.container.enabled",
    )

    if enabled == "false":
        info("Container build disabled (release.container.enabled: false) -- skipping")
        return 0

    dockerfile_name = config.setting("release.container.dockerfile")
    decision = detect(
        language=language,
        project_dir=project_dir,
        dockerfile=dockerfile_name,
    )

    if not decision.build:
        if enabled == "true":
            error(
                f"release.container.enabled: true, but there is no Dockerfile at "
                f"{dockerfile_name} -- hyperi-ci builds images only from a repo "
                "Dockerfile"
            )
            return 1
        if decision.notice:
            warn(f"Container build skipped -- {decision.reason}")
        else:
            info(f"Container build skipped -- {decision.reason}")
        return 0

    info(f"Container build will run -- {decision.reason}")
    _log_builder_cgroups()

    org = load_org_config()
    registry_bases = resolve_registry_bases(org=org)

    push_mode = resolve_push_mode(dev_push=_dev_push_opt_in(_section(config)))
    info(f"Container build from {dockerfile_name} ({push_mode})")

    with group(f"Container Build ({dockerfile_name})"):
        return _build_custom(
            config=config,
            org=org,
            registry_bases=registry_bases,
            push_mode=push_mode,
            dockerfile_name=dockerfile_name,
            language=language,
        )


def _build_custom(
    *,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_mode: str,
    dockerfile_name: str,
    language: str = "",
) -> int:
    dockerfile = Path(dockerfile_name)
    if not dockerfile.exists():
        error(f"Dockerfile not found: {dockerfile}")
        return 1

    # Rust and Go always count as binary-backed; other languages only when the
    # Dockerfile copies dist/ from the build context.
    binary_backed = language in _BINARY_LANGUAGES or bool(
        _DIST_CONTEXT_COPY.search(dockerfile.read_text(encoding="utf-8"))
    )

    return _dispatch_build(
        dockerfile_path=dockerfile,
        config=config,
        org=org,
        registry_bases=registry_bases,
        push_mode=push_mode,
        binary_backed=binary_backed,
    )


def _dispatch_build(
    *,
    dockerfile_path: Path,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_mode: str,
    binary_backed: bool = True,
) -> int:
    container_cfg = _section(config)
    image_name = Path.cwd().name
    try:
        version = _read_version()
    except ReleaseVersionError as exc:
        error(f"Container: {exc}")
        return 1
    sha = _read_sha()
    # `release.channel` states where STABLE artefacts go; a prerelease version
    # ships on the channel its own label names (issue #144).
    channel = effective_release_channel(config.setting("release.channel"), version)

    # Only a GA release-channel push adds `:latest`, so only that one asks.
    move_latest = not (
        push_mode == RELEASE
        and channel == "release"
        and holds_latest(version, "the GHCR :latest tag")
    )

    tags = resolve_tags(
        registry_bases=registry_bases,
        image_name=image_name,
        version=version,
        sha=sha,
        channel=channel,
        mode=push_mode,
        branch_slug=dev_branch_slug() if push_mode == DEV else "",
        move_latest=move_latest,
    )

    from hyperi_ci.description_source import resolve_description
    from hyperi_ci.init import detect_license

    # GHCR renders this label as the package page's description, so an empty
    # one leaves the page blank.
    resolved = resolve_description(config, root=Path.cwd())
    if resolved:
        description, source = resolved
        info(f"Image description from {source}: {description}")
    else:
        description = ""
        warn(
            "No project description found -- the image label and the GHCR "
            "package page will be blank. Add one to the manifest "
            "(Cargo.toml [workspace.package] for a workspace), or set "
            "`description:` in .hyperi-ci.yaml."
        )

    revision = os.environ.get("GITHUB_SHA", _read_sha())
    labels = build_oci_labels(
        repo=f"{org.github_org}/{image_name}",
        revision=revision,
        version=version,
        title=image_name,
        description=description,
        licenses=detect_license(Path.cwd()),
        optimized=not skip_optimize(config),
    )
    cfg_labels = container_cfg.get("labels", {})
    if cfg_labels:
        labels.update(cfg_labels)

    # The same version and revision as the labels, so an ARG and the label agree.
    try:
        build_args = render_build_args(
            config.setting("release.container.build_args"),
            version=version,
            sha=revision,
        )
    except BuildArgError as exc:
        error(str(exc))
        return 1

    platforms = config.setting("release.container.platforms")
    context = config.setting("release.container.context")

    # Outside a release the Build job ships linux-amd64 only, so binary-backed
    # images keep the platforms whose binaries exist and source-built images
    # take one arch.
    if push_mode != RELEASE:
        configured_platforms = list(platforms)
        if binary_backed:
            platforms = _filter_platforms_to_available_binaries(
                platforms=platforms,
                image_name=image_name,
            )
            if not platforms:
                error(
                    f"Container build configured for {configured_platforms} "
                    f"but no matching dist/{image_name}-linux-<arch> binaries "
                    f"present. Build stage failed to produce artefacts OR the "
                    f"Container job can't see them (check "
                    f"actions/upload-artifact + actions/download-artifact "
                    f"version compatibility)."
                )
                return 1
        else:
            platforms = _template_platforms(platforms)
            if platforms != configured_platforms:
                info(
                    f"  Container: source-built {push_mode} build constrained "
                    f"to {platforms} (multi-arch only on release)"
                )

    from hyperi_ci.container.binary_stage import stage_binary_dockerfile

    effective_dockerfile = stage_binary_dockerfile(
        dockerfile_path, context=Path(context)
    )
    rewrote = effective_dockerfile != dockerfile_path

    push = push_mode != VALIDATE
    scratch = tempfile.TemporaryDirectory(prefix="hyperi-ci-buildx-")
    metadata_file = Path(scratch.name) / "metadata.json" if push else None
    attempts, retry_delay = retry_settings(container_cfg)
    try:
        rc = build_and_push(
            dockerfile_path=effective_dockerfile,
            context=context,
            tags=tags,
            platforms=platforms,
            labels=labels,
            build_args=build_args if build_args else None,
            push=push,
            metadata_file=metadata_file,
            attempts=attempts,
            retry_delay=retry_delay,
        )
        if rc == 0 and metadata_file is not None and tags:
            _write_digest_outputs(tags[0], pushed_digest(metadata_file))
    finally:
        scratch.cleanup()
        if rewrote:
            effective_dockerfile.unlink(missing_ok=True)

    if rc == 0 and push_mode == VALIDATE:
        success("Container Dockerfile validated (no push on push-to-main)")
    elif rc == 0 and push_mode == DEV:
        success(
            "Dev image pushed (branch artifact class -- GHCR only, "
            "mutable branch tag; GA publish untouched)"
        )
    return rc


_PLATFORM_TO_OS_ARCH = {
    "linux/amd64": "linux-amd64",
    "linux/arm64": "linux-arm64",
}


def _template_platforms(platforms: list[str]) -> list[str]:
    """Return one platform for a source-built image's validate or dev build.

    Prefers linux/amd64, the runner's native arch (arm64 runs under qemu),
    else the first configured platform.
    """
    if "linux/amd64" in platforms:
        return ["linux/amd64"]
    return list(platforms[:1])


def _filter_platforms_to_available_binaries(
    *,
    platforms: list[str],
    image_name: str,
    dist_dir: Path | None = None,
) -> list[str]:
    """Return the ``platforms`` whose ``dist/<image>-<os>-<arch>`` binary exists.

    Platforms outside :data:`_PLATFORM_TO_OS_ARCH` (e.g. ``linux/s390x``) pass
    through, so a build the project asked for is never dropped silently.
    """
    cwd = dist_dir or Path("dist")
    kept: list[str] = []
    for platform in platforms:
        os_arch = _PLATFORM_TO_OS_ARCH.get(platform)
        if os_arch is None:
            kept.append(platform)
            continue
        candidate = cwd / f"{image_name}-{os_arch}"
        if candidate.exists():
            kept.append(platform)
        else:
            info(
                f"  Container: skipping {platform} -- "
                f"{candidate} not present (not built by current Build job)"
            )
    return kept
