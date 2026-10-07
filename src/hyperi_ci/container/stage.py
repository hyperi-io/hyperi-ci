# Project:   HyperI CI
# File:      src/hyperi_ci/container/stage.py
# Purpose:   Container build stage handler
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container build stage.

The image is always built from the repo's own Dockerfile. Three-state
``release.container.enabled`` gate:

* ``auto`` (default): build when the Dockerfile exists. A library skips
  quietly; a runnable project with no Dockerfile skips with a warning.
* ``true``: build is required; no Dockerfile fails the stage.
* ``false``: explicit skip.

Every container is built and (in release mode) pushed to GHCR.

Push modes (resolved by :mod:`hyperi_ci.release_mode` -- the SSOT):

* ``release``  -- release dispatch / Release-trailer push to main: full
  tag set, pushed.
* ``dev``      -- branch-mode dev image (plan decision 3): mutable
  ``branch-<slug>`` + ``sha-<short>`` tags to GHCR only, behind the
  ``release.container.dev_push`` opt-in on pull_request / branch CI
  runs. Never version tags, never ``latest``.
* ``validate`` -- push-to-main and local runs: build, no push.
"""

import os
import re
import subprocess
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
    render_build_args,
    resolve_tags,
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

# Languages whose Build stage ships per-arch dist/ binaries that custom
# Dockerfiles consume (directly or via the binary_stage COPY rewrite).
_BINARY_LANGUAGES = {"rust", "golang"}

# A COPY/ADD of dist/ from the BUILD CONTEXT = the Dockerfile consumes CI
# build artefacts. `COPY --from=<stage> ... dist/` does NOT count -- that
# is a multi-stage internal path (e.g. tsc compiles to dist/ inside the
# builder stage, the ci-test-ts-app pattern) and needs no CI artefacts.
_DIST_CONTEXT_COPY = re.compile(r"(?m)^\s*(?:COPY|ADD)\s+(?!--from[=\s])[^\n]*\bdist/")


def _read_version() -> str:
    """Resolve the version this container should be tagged with.

    Shares the HYPERCI_VERSION-first resolver with the publish stages (one
    SSoT -- common.resolve_release_version, issue #27). Container needs a
    concrete tag even with no env/VERSION, so it falls back to the ref then
    "0.0.0".
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


def _is_release_mode() -> bool:
    """Return the DEPRECATED bool view (delegates to :mod:`hyperi_ci.release_mode`).

    Kept for out-of-tree callers; in-tree code uses the tri-state
    :func:`hyperi_ci.release_mode.resolve_push_mode` (branch-mode).
    """
    return resolve_push_mode() == RELEASE


def _is_push_to_main() -> bool:
    """Return ``not _is_release_mode()`` (deprecated alias for out-of-tree callers).

    The legacy ``push_to_main`` flag was the validate-only signal,
    named confusingly. Will be removed once consumers update.
    """
    return not _is_release_mode()


def _dev_push_opt_in(container_cfg: dict) -> bool:
    """Return the ``release.container.dev_push`` opt-in, coerced to bool."""
    raw = container_cfg.get("dev_push", False)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return bool(raw)


def should_build_container(config: CIConfig, *, language: str = "") -> tuple[bool, str]:
    """Resolve whether the container stage will build -- filesystem only.

    Mirrors :func:`run`'s gate so the workflow can decide BEFORE booting
    Docker Buildx (issue #33): ``enabled: false`` never builds;
    ``enabled: true`` always builds, and :func:`run` then fails loudly when
    there is no Dockerfile; ``enabled: auto`` builds iff the Dockerfile
    exists. A repo with none never pulls buildkit from Docker Hub nor logs
    in to GHCR.

    Returns ``(build, reason)``.
    """
    container_cfg = config.get("release.container", {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}
    enabled = normalise_tristate(
        container_cfg.get("enabled", "auto"), key="release.container.enabled"
    )
    if enabled == "false":
        return False, "release.container.enabled: false"
    if enabled == "true":
        return True, "release.container.enabled: true"
    decision = detect(
        language=language,
        project_dir=Path.cwd(),
        dockerfile=container_cfg.get("dockerfile", "Dockerfile"),
    )
    return decision.build, decision.reason


def _write_output(key: str, value: str) -> None:
    """Append ``key=value`` to ``$GITHUB_OUTPUT`` when it is set."""
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{key}={value}\n")


def _log_builder_cgroups() -> None:
    """Name the cgroup parent each buildx builder got, so a run log shows it."""
    if not is_github_actions():
        return
    builders = builder_cgroup_parents()
    if not builders:
        info("Buildx builder cgroup: no docker-container builder found")
    for name, parent in builders:
        info(f"Buildx builder cgroup: {name} under {parent or 'the daemon default'}")


def _confine_build_paths(container_cfg: dict, project_dir: Path) -> bool:
    """Refuse a Dockerfile or build context outside the checkout.

    Either one hands docker a file the repo does not hold, on a runner whose
    ``~/.docker/config.json`` holds the registry logins.
    """
    try:
        for key, default in (("dockerfile", "Dockerfile"), ("context", ".")):
            confine(
                str(container_cfg.get(key, default)),
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
    container_cfg = config.get("release.container", {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}
    project_dir = Path.cwd()
    # Checked in the resolve step too, so a refusal lands before any login.
    if not _confine_build_paths(container_cfg, project_dir):
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

    # Resolve-only: emit the build decision for the workflow to gate Docker
    # setup on, then return without any Docker work (issue #33). Keeps
    # libraries from booting Buildx / touching GHCR at all.
    if os.environ.get("HYPERCI_CONTAINER_RESOLVE_ONLY"):
        build, reason = should_build_container(config, language=language)
        info(f"Container resolve: build={'true' if build else 'false'} -- {reason}")
        _write_output("build", "true" if build else "false")
        return 0

    enabled = normalise_tristate(
        container_cfg.get("enabled", "auto"), key="release.container.enabled"
    )

    if enabled == "false":
        info("Container build disabled (release.container.enabled: false) -- skipping")
        return 0

    dockerfile_name = container_cfg.get("dockerfile", "Dockerfile")
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

    push_mode = resolve_push_mode(dev_push=_dev_push_opt_in(container_cfg))
    info(f"Container build from {dockerfile_name} ({push_mode})")

    with group(f"Container Build ({dockerfile_name})"):
        return _build_custom(
            container_cfg=container_cfg,
            config=config,
            org=org,
            registry_bases=registry_bases,
            push_mode=push_mode,
            dockerfile_name=dockerfile_name,
            language=language,
        )


def _build_custom(
    *,
    container_cfg: dict,
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

    # Binary languages (rust/go) conventionally consume dist/ binaries in
    # custom Dockerfiles -- even bare `COPY <app>` lines get rewritten to
    # dist paths (binary_stage) -- so they ALWAYS keep the dist filter and
    # its loud artefact-handoff failure. For source languages a custom
    # Dockerfile is only binary-backed if it copies dist/ from the BUILD
    # CONTEXT (e.g. shipping a compiled sidecar); a python/node Dockerfile
    # running pip/npm install -- including multi-stage builds whose
    # internal compile output happens to be named dist/ -- has no CI dist
    # binaries to filter on.
    binary_backed = language in _BINARY_LANGUAGES or bool(
        _DIST_CONTEXT_COPY.search(dockerfile.read_text(encoding="utf-8"))
    )

    return _dispatch_build(
        dockerfile_path=dockerfile,
        container_cfg=container_cfg,
        config=config,
        org=org,
        registry_bases=registry_bases,
        push_mode=push_mode,
        binary_backed=binary_backed,
    )


def _dispatch_build(
    *,
    dockerfile_path: Path,
    container_cfg: dict,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_mode: str,
    binary_backed: bool = True,
) -> int:
    image_name = Path.cwd().name
    try:
        version = _read_version()
    except ReleaseVersionError as exc:
        error(f"Container: {exc}")
        return 1
    sha = _read_sha()
    # `release.channel` states where STABLE artefacts go; a prerelease version
    # ships on the channel its own label names (issue #144).
    channel = effective_release_channel(
        config.get("release.channel", "release"), version
    )

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
            container_cfg.get("build_args"), version=version, sha=revision
        )
    except BuildArgError as exc:
        error(str(exc))
        return 1

    platforms = container_cfg.get("platforms", ["linux/amd64", "linux/arm64"])
    context = container_cfg.get("context", ".")

    # Outside a GA publish the Build job only produces linux-amd64 (saves
    # CI time on push-to-main validates AND branch dev builds).
    #
    # Binary-backed images (the Dockerfile COPYs from
    # dist/<name>-linux-<arch>): constrain to platforms whose binaries are
    # actually present, and fail loud when NONE are (broken Build ->
    # Container artefact handoff).
    #
    # Source-built images (python/node -- built from SOURCE inside the
    # Dockerfile) have no dist/ binaries AT ALL, so the dist filter would
    # always come up empty and hard-fail. Constrain them to a single arch
    # instead -- same only-shipping-runs-pay-for-arm64 doctrine, decided
    # explicitly rather than via dist contents.
    if push_mode != RELEASE:
        configured_platforms = list(platforms)
        if binary_backed:
            platforms = _filter_platforms_to_available_binaries(
                platforms=platforms,
                image_name=image_name,
            )
            if not platforms:
                # No silent-success -- if the project has container builds
                # enabled, missing binaries means the Build -> Container
                # artefact handoff is broken. Fail loud so we never report
                # "container green" without actually producing an image.
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

    # Bare `COPY <app> ...` lines in the Dockerfile reference a file in
    # the build context root that the upstream Build stage doesn't put
    # there -- it puts arch-suffixed binaries in `dist/<app>-linux-<arch>`.
    # Rewrite the Dockerfile to use ${TARGETARCH} substitution so multi-arch
    # buildx works in a single invocation. The same rewrite appends the copy
    # of the context's licence file into /licenses/.
    from hyperi_ci.container.binary_stage import stage_binary_dockerfile

    effective_dockerfile = stage_binary_dockerfile(
        dockerfile_path, context=Path(context)
    )
    rewrote = effective_dockerfile != dockerfile_path

    try:
        rc = build_and_push(
            dockerfile_path=effective_dockerfile,
            context=context,
            tags=tags,
            platforms=platforms,
            labels=labels,
            build_args=build_args if build_args else None,
            push=push_mode != VALIDATE,
        )
    finally:
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
    """Single-arch subset for a source-built image's validate/dev builds.

    Prefers linux/amd64 (the runner's native arch -- arm64 would go via
    qemu); falls back to the first configured platform when amd64 isn't
    configured at all. Never empty for a non-empty input.
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
    """Drop platforms whose pre-built binary is missing from ``dist/``.

    On push-to-main the Build job builds a single arch by default
    (saves CI time); on workflow_dispatch it builds the full matrix.
    Multi-arch buildx fails on push-to-main with "binary not found"
    when one architecture's artefact is absent.

    Returns the subset of ``platforms`` whose corresponding binary
    exists in ``dist/``. Platforms not in the os-arch map (e.g.
    ``linux/s390x`` or future targets) pass through unchanged so we
    don't silently drop a build the project actually wants.
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
