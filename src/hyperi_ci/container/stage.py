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

``HYPERCI_CONTAINER_MODE`` splits one build into native per-arch jobs:

* unset -- one multi-platform build, tagged and pushed.
* ``plan``  -- write ``build``, ``platforms``, ``push``, ``reuse`` and
  ``created`` outputs. Builds nothing.
* ``leg``   -- build ``HYPERCI_CONTAINER_PLATFORM`` alone and, on a push,
  push it by digest to ``HYPERCI_CONTAINER_DIGEST_DIR/<arch>``.
* ``merge`` -- tag every leg digest as one image index.
"""

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.ci_env import is_ci, machine_arch
from hyperi_ci.common import (
    ReleaseVersionError,
    error,
    group,
    holds_latest,
    info,
    is_github_actions,
    normalise_tristate,
    resolve_release_version,
    run_cmd,
    set_github_output,
    skip_optimize,
    success,
    warn,
)
from hyperi_ci.config import CIConfig, OrgConfig, load_org_config
from hyperi_ci.container.build import (
    BuildArgError,
    PublishedImage,
    build_and_push,
    built_natively,
    imagetools_create_command,
    published_image,
    push_by_digest_output,
    pushed_digest,
    render_build_args,
    resolve_tags,
    retry_settings,
)
from hyperi_ci.container.cgroup import builder_cgroup_parents, probe_cgroup_parent
from hyperi_ci.container.detect import detect
from hyperi_ci.container.labels import (
    build_oci_labels,
    labels_to_index_annotation_args,
    oci_created,
)
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

PLAN = "plan"
LEG = "leg"
MERGE = "merge"
_MODES = (PLAN, LEG, MERGE)

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _read_version() -> str:
    """Return the image version: the release version, else the ref, else 0.0.0.

    The fallbacks exist because an image needs a concrete tag even when
    :func:`resolve_release_version` finds none.
    """
    return resolve_release_version() or os.environ.get(
        "GITHUB_REF_NAME", "0.0.0"
    ).removeprefix("v")


def _checkout_sha() -> str:
    """Return the full sha of the commit checked out, else ``GITHUB_SHA``.

    A tag dispatch runs on the default branch, so ``GITHUB_SHA`` names that
    branch's tip while the checkout, and the image, is the tag.
    """
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    head = result.stdout.strip() if result.returncode == 0 else ""
    return head or os.environ.get("GITHUB_SHA") or "unknown"


def _read_sha() -> str:
    return _checkout_sha()[:8]


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


def _write_digest_outputs(version_tag: str, digest: str | None) -> None:
    """Expose the pushed image as ``digest`` and ``image`` (``<tag>@<digest>``)."""
    if digest is None:
        warn(f"buildx recorded no digest for {version_tag}; no image output is set")
        return
    info(f"Pushed image: {version_tag}@{digest}")
    set_github_output(digest=digest, image=f"{version_tag}@{digest}")


def _create_index(
    tags: list[str], sources: list[str], annotations: list[str] | None = None
) -> bool:
    """Point ``tags`` at ``sources`` in one registry write, True on success."""
    cmd = imagetools_create_command(tags=tags, sources=sources, annotations=annotations)
    target = ", ".join(sources)
    try:
        result = run_cmd(cmd, check=False, capture=True)
    except OSError as exc:
        error(f"Could not point {', '.join(tags)} at {target}: {exc}")
        return False
    if result.returncode != 0:
        error(f"Could not point {', '.join(tags)} at {target}:")
        error(result.stderr or "")
        return False
    return True


def _reusable(
    version_tag: str,
    image: PublishedImage | None,
    revision: str,
    platforms: list[str],
) -> bool:
    """Return True when ``image`` at ``version_tag`` already ships this run.

    A rebuild of the same commit gets a new digest and moves the version tag,
    leaving a chart already published for that version pinned to an untagged
    image. An image from another commit, or one that names none, is the
    leftover of a release that never tagged, and one missing a platform this
    run builds is incomplete. Both are rebuilt as before.
    """
    if image is None:
        return False
    if image.revision != revision:
        warn(
            f"{version_tag} already holds an image built from "
            f"{image.revision or 'an unrecorded commit'}; this run builds "
            f"{revision} and moves the tag."
        )
        return False
    missing = sorted(set(platforms) - image.platforms)
    if missing:
        warn(
            f"{version_tag} is already published from this commit but has no "
            f"{', '.join(missing)} image; rebuilding it with every platform."
        )
        return False
    if image.native is not None and (emulated := sorted(set(platforms) - image.native)):
        warn(
            f"{version_tag} is already published from this commit, but its "
            f"provenance does not show {', '.join(emulated)} built on its own "
            "arch; rebuilding it natively."
        )
        return False
    info(f"{version_tag} is already published from this commit: reusing it")
    return True


def _reuse_published(
    tags: list[str],
    image: PublishedImage | None,
    revision: str,
    platforms: list[str],
) -> int | None:
    """Hand on the image this commit already published at ``version_tag``.

    The run's other tags are pointed at the reused digest, since the earlier
    attempt may have died before pushing them.

    Returns:
        None when the image is to be built (:func:`_reusable` is False), else
        the stage's exit code.

    """
    version_tag = tags[0]
    if image is None or not _reusable(version_tag, image, revision, platforms):
        return None
    others = tags[1:]
    if others and not _create_index(
        others, [f"{version_tag.rpartition(':')[0]}@{image.digest}"]
    ):
        return 1
    set_github_output(digest=image.digest, image=f"{version_tag}@{image.digest}")
    return 0


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
        set_github_output(**{"cgroup-parent": found.parent or ""})
        return 0

    try:
        mode = _container_mode()
    except ValueError as exc:
        error(str(exc))
        return 1

    # The workflow gates Docker setup on this output, so it does no Docker work.
    # Plan mode is a superset, so a CLI release without it still only resolves.
    if os.environ.get("HYPERCI_CONTAINER_RESOLVE_ONLY") or mode == PLAN:
        build, reason = should_build_container(config, language=language)
        info(f"Container resolve: build={'true' if build else 'false'} -- {reason}")
        set_github_output(build="true" if build else "false")
        if mode != PLAN:
            return 0
        if not build:
            set_github_output(platforms="[]", push="false", reuse="false", created="")
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
    if mode in (None, LEG):
        _log_builder_cgroups()

    org = load_org_config()
    registry_bases = resolve_registry_bases(org=org)

    push_mode = resolve_push_mode(dev_push=_dev_push_opt_in(_section(config)))
    info(f"Container build from {dockerfile_name} ({push_mode})")

    title = "Build" if mode is None else mode.capitalize()
    with group(f"Container {title} ({dockerfile_name})"):
        return _build_custom(
            config=config,
            org=org,
            registry_bases=registry_bases,
            push_mode=push_mode,
            dockerfile_name=dockerfile_name,
            language=language,
            mode=mode,
        )


def _build_custom(
    *,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_mode: str,
    dockerfile_name: str,
    language: str = "",
    mode: str | None = None,
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
        mode=mode,
    )


@dataclass(frozen=True, slots=True)
class _ImageSpec:
    """What every mode of one run tags, labels and builds."""

    image_name: str
    repo: str
    revision: str
    tags: list[str]
    labels: dict[str, str]
    build_args: dict[str, str]
    platforms: list[str]
    context: str
    push_mode: str
    container_cfg: dict


def _image_spec(
    *,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_mode: str,
    created: str | None = None,
) -> _ImageSpec | None:
    """Return the tags, labels and build args of this run, None after an error."""
    container_cfg = _section(config)
    image_name = Path.cwd().name
    try:
        version = _read_version()
    except ReleaseVersionError as exc:
        error(f"Container: {exc}")
        return None
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

    revision = _checkout_sha()
    labels = build_oci_labels(
        repo=f"{org.github_org}/{image_name}",
        revision=revision,
        version=version,
        title=image_name,
        description=description,
        licenses=detect_license(Path.cwd()),
        optimized=not skip_optimize(config),
        created=created,
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
        return None

    return _ImageSpec(
        image_name=image_name,
        repo=f"{registry_bases[0]}/{image_name}" if registry_bases else "",
        revision=revision,
        tags=tags,
        labels=labels,
        build_args=build_args,
        platforms=config.setting("release.container.platforms"),
        context=config.setting("release.container.context"),
        push_mode=push_mode,
        container_cfg=container_cfg,
    )


def _select_platforms(
    spec: _ImageSpec, platforms: list[str], *, binary_backed: bool
) -> list[str] | None:
    """Return the platforms this run builds out of ``platforms``, None after an error.

    Outside a release the Build job ships linux-amd64 only, so binary-backed
    images keep the platforms whose binaries exist and source-built images
    take one arch.
    """
    if spec.push_mode == RELEASE:
        return platforms
    configured_platforms = list(platforms)
    if binary_backed:
        kept = _filter_platforms_to_available_binaries(
            platforms=platforms,
            image_name=spec.image_name,
        )
        if not kept:
            error(
                f"Container build configured for {configured_platforms} "
                f"but no matching dist/{spec.image_name}-linux-<arch> binaries "
                f"present. Build stage failed to produce artefacts OR the "
                f"Container job can't see them (check "
                f"actions/upload-artifact + actions/download-artifact "
                f"version compatibility)."
            )
            return None
        return kept
    kept = _template_platforms(platforms)
    if kept != configured_platforms:
        info(
            f"  Container: source-built {spec.push_mode} build constrained "
            f"to {kept} (multi-arch only on release)"
        )
    return kept


def _container_mode() -> str | None:
    """Return ``HYPERCI_CONTAINER_MODE``, None when it is unset.

    Raises:
        ValueError: The variable names no mode.

    """
    raw = os.environ.get("HYPERCI_CONTAINER_MODE", "").strip()
    if not raw:
        return None
    if raw not in _MODES:
        raise ValueError(
            f"HYPERCI_CONTAINER_MODE={raw!r} is not a container mode -- use one "
            f"of {', '.join(_MODES)}, or leave it unset for one multi-platform build"
        )
    return raw


def _digest_name(platform: str) -> str:
    """Return the digest file name for ``platform``: ``linux/arm/v7`` is ``arm-v7``."""
    return platform.removeprefix("linux/").replace("/", "-")


def _merge_platforms(raw: str) -> list[str] | None:
    """Return the plan's ``platforms`` JSON as a list, None when it is not one."""
    try:
        platforms = json.loads(raw)
    except ValueError:
        platforms = None
    valid = (
        isinstance(platforms, list)
        and bool(platforms)
        and all(isinstance(p, str) and p for p in platforms)
        and len({_digest_name(p) for p in platforms}) == len(platforms)
    )
    if not valid:
        error(
            f"HYPERCI_CONTAINER_PLATFORMS={raw!r} is not a JSON list of distinct "
            "platforms, the plan's platforms output"
        )
        return None
    return platforms


def _plan(spec: _ImageSpec, *, binary_backed: bool) -> int:
    """Write the outputs the per-arch legs and the merge run on, building nothing."""
    reuse = (
        spec.push_mode == RELEASE
        and bool(spec.tags)
        and _reusable(
            spec.tags[0], published_image(spec.tags[0]), spec.revision, spec.platforms
        )
    )
    platforms = _select_platforms(spec, spec.platforms, binary_backed=binary_backed)
    if platforms is None:
        return 1
    push = spec.push_mode != VALIDATE
    created = oci_created()
    info(
        f"Container plan: platforms={', '.join(platforms)} push={str(push).lower()} "
        f"reuse={str(reuse).lower()} created={created}"
    )
    set_github_output(
        platforms=json.dumps(platforms, separators=(",", ":")),
        push="true" if push else "false",
        reuse="true" if reuse else "false",
        created=created,
    )
    return 0


def _leg_digests(digest_dir: Path, platforms: list[str]) -> list[str] | None:
    """Return each leg's pushed digest in ``platforms`` order, None after an error."""
    expected = {_digest_name(p): p for p in platforms}
    try:
        present = {entry.name for entry in digest_dir.iterdir()}
    except OSError as exc:
        error(f"Cannot read the leg digests in {digest_dir}: {exc}")
        return None
    missing = sorted(set(expected) - present)
    extra = sorted(present - set(expected))
    if missing or extra:
        error(
            f"The leg digests in {digest_dir} do not match the planned platforms "
            f"-- missing: {', '.join(missing) or 'none'}, extra: "
            f"{', '.join(extra) or 'none'}"
        )
        return None
    digests: list[str] = []
    for name, platform in expected.items():
        path = digest_dir / name
        try:
            digest = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            error(f"Cannot read the {platform} digest at {path}: {exc}")
            return None
        if not _SHA256_DIGEST.fullmatch(digest):
            error(f"{path} holds no sha256 digest for {platform}: {digest!r}")
            return None
        digests.append(digest)
    return digests


def _merge(
    spec: _ImageSpec, *, digest_dir: Path, platforms: list[str], reuse: bool
) -> int:
    """Name every leg's digest under this run's tags in one image index."""
    if spec.push_mode == VALIDATE or not spec.tags:
        error(
            f"Merge mode tags a pushed image, but this run is {spec.push_mode} "
            "mode and has no tags to apply"
        )
        return 1
    version_tag = spec.tags[0]
    if reuse:
        reused = _reuse_published(
            spec.tags, published_image(version_tag), spec.revision, platforms
        )
        if reused is None:
            error(
                f"The plan reused {version_tag}, but it no longer holds this "
                "commit's image on every platform, and no leg built one"
            )
            return 1
        return reused

    digests = _leg_digests(digest_dir, platforms)
    if digests is None:
        return 1
    # One source with no annotations is copied as it stands, the same image a
    # single-platform build pushes.
    annotations = (
        labels_to_index_annotation_args(spec.labels) if len(platforms) > 1 else []
    )
    sources = [f"{spec.repo}@{digest}" for digest in digests]
    # Before any tag moves, so an emulated leg never reaches a released tag.
    emulated = [
        platform
        for platform, source in zip(platforms, sources, strict=True)
        if not built_natively(source, platform)
    ]
    if emulated:
        error(
            f"The {', '.join(emulated)} leg's provenance does not show it built on "
            "its own arch, so it is not tagged. Rebuild it on a native runner."
        )
        return 1
    if not _create_index(spec.tags, sources, annotations):
        return 1

    image = published_image(version_tag)
    if image is None:
        error(f"{version_tag} was written but the registry cannot read it back")
        return 1
    missing = sorted(set(platforms) - image.platforms)
    if image.revision != spec.revision or missing:
        error(
            f"{version_tag} reads back as revision {image.revision or 'none'} "
            f"missing {', '.join(missing) or 'no platform'}, where this run "
            f"merged {spec.revision} for {', '.join(platforms)}"
        )
        return 1
    _write_digest_outputs(version_tag, image.digest)
    success(f"Merged {', '.join(platforms)} as {', '.join(spec.tags)}")
    return 0


def _dispatch_build(
    *,
    dockerfile_path: Path,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_mode: str,
    binary_backed: bool = True,
    mode: str | None = None,
) -> int:
    leg_platform = os.environ.get("HYPERCI_CONTAINER_PLATFORM", "").strip()
    created = os.environ.get("HYPERCI_CONTAINER_CREATED", "").strip() or None
    digest_env = os.environ.get("HYPERCI_CONTAINER_DIGEST_DIR", "").strip()
    digest_dir = Path(digest_env) if digest_env else None

    if mode in (LEG, MERGE) and len(registry_bases) != 1:
        error(f"{mode} mode pushes to one registry, not {registry_bases}")
        return 1
    if mode == LEG:
        needed = {
            "HYPERCI_CONTAINER_PLATFORM": leg_platform,
            "HYPERCI_CONTAINER_CREATED": created,
        }
        if push_mode != VALIDATE:
            needed["HYPERCI_CONTAINER_DIGEST_DIR"] = digest_env
        unset = [name for name, value in needed.items() if not value]
        if unset:
            error(f"Leg mode needs {', '.join(unset)} set")
            return 1
    merge_platforms: list[str] = []
    if mode == MERGE:
        raw_platforms = os.environ.get("HYPERCI_CONTAINER_PLATFORMS", "").strip()
        if digest_dir is None or not raw_platforms:
            error(
                "Merge mode needs HYPERCI_CONTAINER_DIGEST_DIR and "
                "HYPERCI_CONTAINER_PLATFORMS set"
            )
            return 1
        parsed = _merge_platforms(raw_platforms)
        if parsed is None:
            return 1
        merge_platforms = parsed

    spec = _image_spec(
        config=config,
        org=org,
        registry_bases=registry_bases,
        push_mode=push_mode,
        created=created if mode in (LEG, MERGE) else None,
    )
    if spec is None:
        return 1

    if mode == PLAN:
        return _plan(spec, binary_backed=binary_backed)
    if mode == MERGE and digest_dir is not None:
        reuse = os.environ.get("HYPERCI_CONTAINER_REUSE", "").strip() == "true"
        return _merge(
            spec, digest_dir=digest_dir, platforms=merge_platforms, reuse=reuse
        )

    if mode is None and push_mode == RELEASE and spec.tags:
        reused = _reuse_published(
            spec.tags, published_image(spec.tags[0]), spec.revision, spec.platforms
        )
        if reused is not None:
            return reused

    requested = [leg_platform] if mode == LEG else spec.platforms
    platforms = _select_platforms(spec, requested, binary_backed=binary_backed)
    if platforms is None:
        return 1
    if is_ci() and _refuse_emulation(platforms):
        return 1

    from hyperi_ci.container.binary_stage import stage_binary_dockerfile

    effective_dockerfile = stage_binary_dockerfile(
        dockerfile_path, context=Path(spec.context)
    )
    rewrote = effective_dockerfile != dockerfile_path

    push = push_mode != VALIDATE
    # A leg pushes by digest alone, so the merge applies every tag at once.
    by_digest = mode == LEG and push
    scratch = tempfile.TemporaryDirectory(prefix="hyperi-ci-buildx-")
    metadata_file = Path(scratch.name) / "metadata.json" if push else None
    attempts, retry_delay = retry_settings(spec.container_cfg)
    try:
        rc = build_and_push(
            dockerfile_path=effective_dockerfile,
            context=spec.context,
            tags=[] if mode == LEG else spec.tags,
            platforms=platforms,
            labels=spec.labels,
            build_args=spec.build_args if spec.build_args else None,
            push=push and not by_digest,
            metadata_file=metadata_file,
            attempts=attempts,
            retry_delay=retry_delay,
            output=push_by_digest_output(spec.repo) if by_digest else None,
        )
        if rc == 0 and by_digest and metadata_file is not None and digest_dir:
            rc = _write_leg_digest(
                digest_dir, leg_platform, pushed_digest(metadata_file)
            )
        elif rc == 0 and metadata_file is not None and spec.tags:
            _write_digest_outputs(spec.tags[0], pushed_digest(metadata_file))
    finally:
        scratch.cleanup()
        if rewrote:
            effective_dockerfile.unlink(missing_ok=True)

    if rc == 0 and push_mode == VALIDATE:
        success("Container Dockerfile validated (no push on push-to-main)")
    elif rc == 0 and push_mode == DEV and mode is None:
        success(
            "Dev image pushed (branch artifact class -- GHCR only, "
            "mutable branch tag; GA publish untouched)"
        )
    return rc


def _refuse_emulation(platforms: list[str], machine: str | None = None) -> bool:
    """Return True, with an error, when a platform is not this runner's own arch.

    BuildKit falls back to its bundled QEMU for a foreign platform, so the only
    check that the build is native is the runner's CPU.
    """
    native = f"linux/{machine_arch(machine)}"
    foreign = [p for p in platforms if p != native]
    if foreign:
        error(
            f"Refusing to build {', '.join(foreign)} on a {native} runner: it would "
            "run under emulation. Build each arch on its own runner (leg mode)."
        )
    return bool(foreign)


def _write_leg_digest(digest_dir: Path, platform: str, digest: str | None) -> int:
    """Write ``digest`` to ``digest_dir/<arch>`` for the merge, returning an exit code."""
    if digest is None:
        error(
            f"buildx recorded no digest for the {platform} image, so the merge "
            "would have nothing to name"
        )
        return 1
    path = digest_dir / _digest_name(platform)
    try:
        digest_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{digest}\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        error(f"Could not write the {platform} digest to {path}: {exc}")
        return 1
    info(f"Pushed {platform} by digest {digest}, recorded in {path}")
    return 0


_PLATFORM_TO_OS_ARCH = {
    "linux/amd64": "linux-amd64",
    "linux/arm64": "linux-arm64",
}


def _template_platforms(platforms: list[str]) -> list[str]:
    """Return one platform for a source-built image's validate or dev build.

    Prefers linux/amd64, the x64 runners' own arch,
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
