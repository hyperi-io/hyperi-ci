# Project:   HyperI CI
# File:      src/hyperi_ci/container/build.py
# Purpose:   Docker buildx build and push execution
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Execute docker buildx build with optional multi-registry push."""

import json
import string
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from hyperi_ci.common import echo_chunk, error, info, stream_cmd, success, warn
from hyperi_ci.container.labels import (
    labels_to_build_args,
    labels_to_index_annotation_args,
)
from hyperi_ci.release_branches import is_prerelease_version

_BUILD_ARG_PLACEHOLDERS = ("version", "sha")

# apt files these under errAuthErr (apt-pkg/acquire-worker.cc), which it never
# retries, so only a rebuild once the mirror has finished syncing clears them.
MIRROR_MARKERS = (
    "Mirror sync in progress",
    "Hash Sum mismatch",
    "File has unexpected size",
)

# Last resort when `release.container` carries neither key; defaults.yaml owns them.
_DEFAULT_BUILD_ATTEMPTS = 3
_DEFAULT_RETRY_DELAY_SECONDS = 60.0


def mirror_marker(output: str) -> str | None:
    """Return the first :data:`MIRROR_MARKERS` entry found in ``output``, or None."""
    return next((m for m in MIRROR_MARKERS if m in output), None)


def retry_settings(container_cfg: Mapping[str, object]) -> tuple[int, float]:
    """Return ``(attempts, delay_seconds)`` from ``release.container``.

    A value that is not a number, or is below 1 attempt or 0 seconds, falls
    back to the default rather than failing the build.
    """
    attempts = container_cfg.get("build_attempts", _DEFAULT_BUILD_ATTEMPTS)
    delay = container_cfg.get("build_retry_delay_seconds", _DEFAULT_RETRY_DELAY_SECONDS)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        attempts = _DEFAULT_BUILD_ATTEMPTS
    if isinstance(delay, bool) or not isinstance(delay, int | float) or delay < 0:
        delay = _DEFAULT_RETRY_DELAY_SECONDS
    return attempts, float(delay)


class BuildArgError(ValueError):
    """A ``release.container.build_args`` value cannot be rendered."""


def render_build_args(
    build_args: Mapping[str, object] | None, *, version: str, sha: str
) -> dict[str, str]:
    """Substitute ``{version}`` and ``{sha}`` into build-arg values.

    Values follow ``str.format`` brace rules: ``{{`` and ``}}`` are a literal
    brace. Keys are never substituted. Only the two bare placeholders are
    accepted, so a typo such as ``{verison}`` fails the build instead of
    shipping the literal text.

    Args:
        build_args: The ``release.container.build_args`` mapping, or None.
        version: The version the image's ``org.opencontainers.image.version``
            label carries.
        sha: The commit the image's ``org.opencontainers.image.revision``
            label carries.

    Returns:
        The mapping with every value rendered to a string.

    Raises:
        BuildArgError: ``build_args`` is not a mapping, or a value holds an
            unknown placeholder or an unbalanced brace.

    """
    if build_args is None:
        return {}
    if not isinstance(build_args, Mapping):
        raise BuildArgError(
            "release.container.build_args must be a mapping of ARG name to "
            f"value, got {type(build_args).__name__}"
        )

    known = {"version": version, "sha": sha}
    allowed = ", ".join(f"{{{name}}}" for name in _BUILD_ARG_PLACEHOLDERS)
    rendered: dict[str, str] = {}
    for key, raw in build_args.items():
        text = str(raw)
        where = f"release.container.build_args.{key} = {text!r}"
        try:
            parts = list(string.Formatter().parse(text))
        except ValueError as exc:
            raise BuildArgError(
                f"{where}: {exc}. Write a literal brace as {{{{ or }}}}."
            ) from exc

        pieces: list[str] = []
        for literal, field, spec, conversion in parts:
            pieces.append(literal)
            if field is None:
                continue
            if field not in known or spec or conversion:
                token = field
                if conversion:
                    token += f"!{conversion}"
                if spec:
                    token += f":{spec}"
                raise BuildArgError(
                    f"{where}: unknown placeholder {{{token}}}. Supported: "
                    f"{allowed}. Write a literal brace as {{{{ or }}}}."
                )
            pieces.append(known[field])
        rendered[str(key)] = "".join(pieces)
    return rendered


def buildx_command(
    *,
    dockerfile_path: Path,
    context: str = ".",
    tags: list[str],
    platforms: list[str],
    labels: dict[str, str],
    build_args: dict[str, str] | None = None,
    push: bool = True,
    metadata_file: Path | None = None,
) -> list[str]:
    """Return the ``docker buildx build`` argv :func:`build_and_push` runs.

    A multi-platform build also carries the ``org.opencontainers.image.*``
    labels as index annotations, which is where GHCR reads a multi-arch
    package's description from.

    Args:
        dockerfile_path: Path to the Dockerfile.
        context: Docker build context directory. Always the last argument.
        tags: Full image tags spanning all target registries.
        platforms: Target platforms (e.g. ``["linux/amd64", "linux/arm64"]``).
        labels: OCI labels dict.
        build_args: Additional ``--build-arg key=value`` pairs, already
            rendered by :func:`render_build_args`.
        push: When True, append ``--push``.
        metadata_file: Where buildx writes its result metadata, which
            carries the pushed image's digest.

    Returns:
        The argv list.

    """
    cmd = [
        "docker",
        "buildx",
        "build",
        "--file",
        str(dockerfile_path),
        "--platform",
        ",".join(platforms),
    ]

    for tag in tags:
        cmd.extend(["--tag", tag])

    cmd.extend(labels_to_build_args(labels))

    # BuildKit refuses index annotations on a single-platform export.
    if len(platforms) > 1:
        cmd.extend(labels_to_index_annotation_args(labels))

    if build_args:
        for key, value in sorted(build_args.items()):
            cmd.extend(["--build-arg", f"{key}={value}"])

    if metadata_file is not None:
        cmd.extend(["--metadata-file", str(metadata_file)])

    if push:
        cmd.append("--push")
    # Never --load: the local daemon takes one platform, and build-and-discard
    # still validates every layer.

    cmd.append(context)
    return cmd


def build_and_push(
    *,
    dockerfile_path: Path,
    context: str = ".",
    tags: list[str],
    platforms: list[str],
    labels: dict[str, str],
    build_args: dict[str, str] | None = None,
    push: bool = True,
    metadata_file: Path | None = None,
    attempts: int = 1,
    retry_delay: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Build a container image with docker buildx and optionally push.

    With ``push`` False the image is built and discarded: every layer still
    runs, but nothing leaves the runner.

    A failed build whose output carries a :data:`MIRROR_MARKERS` entry is run
    again, up to ``attempts`` in all. BuildKit's layer cache means the re-run
    redoes only the failed layer. Any other failure returns at once.

    Args:
        dockerfile_path: Path to the Dockerfile.
        context: Docker build context directory.
        tags: Full image tags spanning all target registries.
        platforms: Target platforms (e.g. ``["linux/amd64", "linux/arm64"]``).
        labels: OCI labels dict.
        build_args: Additional ``--build-arg key=value`` pairs, already
            rendered by :func:`render_build_args`.
        push: Push to every tagged registry when True.
        metadata_file: Where buildx writes its result metadata; read it
            back with :func:`pushed_digest`.
        attempts: Total builds to try when an apt mirror is mid-sync.
        retry_delay: Seconds to wait before each rebuild.
        sleep: The wait, injectable so a test does not block.

    Returns:
        Exit code (0 = success).

    """
    cmd = buildx_command(
        dockerfile_path=dockerfile_path,
        context=context,
        tags=tags,
        platforms=platforms,
        labels=labels,
        build_args=build_args,
        push=push,
        metadata_file=metadata_file,
    )

    action = "Pushing" if push else "Validating (no push)"
    info(f"{action}: {', '.join(tags) if tags else '<no tags>'}")
    info(f"Platforms: {', '.join(platforms)}")

    returncode = 0
    for attempt in range(1, max(attempts, 1) + 1):
        returncode, tail = stream_cmd(cmd, on_line=None, on_chunk=echo_chunk)
        if returncode == 0:
            break
        marker = mirror_marker(tail)
        if marker is None or attempt >= attempts:
            break
        warn(
            f"apt mirror looks mid-sync ('{marker}' in the build output), "
            f"rebuilding in {retry_delay:g}s (attempt {attempt + 1} of {attempts})"
        )
        sleep(retry_delay)

    if returncode != 0:
        error("docker buildx build failed")
        return returncode

    action = "pushed" if push else "validated"
    if tags:
        success(f"Built and {action}: {tags[0]}")
    else:
        success(f"Built and {action}")
    return 0


def pushed_digest(metadata_file: Path) -> str | None:
    """Return the ``sha256:`` digest buildx recorded for the pushed image.

    A multi-platform push records the index digest, which is the one a
    ``<image>@<digest>`` reference has to name.
    """
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digest = (
        metadata.get("containerimage.digest") if isinstance(metadata, dict) else None
    )
    return digest if isinstance(digest, str) and digest.startswith("sha256:") else None


def resolve_tags(
    *,
    registry_bases: list[str],
    image_name: str,
    version: str,
    sha: str,
    channel: str = "release",
    mode: str = "release",
    branch_slug: str = "",
    move_latest: bool = True,
) -> list[str]:
    """Generate image tags spanning all configured registries.

    Tags per registry base, by push mode (:mod:`hyperi_ci.release_mode`):

    * ``validate`` -> none.
    * ``dev`` -> ``:branch-<slug>`` and ``:branch-<slug>-sha-<short>``. Never
      a version, ``latest`` or bare ``sha-<short>``: the ``branch-*`` and
      ``dev-sha-*`` prefixes let ``_ghcr-prune.yml`` glob dev tags without
      touching GA pins.
    * ``release``, release channel -> ``:vX.Y.Z``, ``:latest``, ``:sha-<short>``
    * ``release``, pre-GA channel  -> ``:vX.Y.Z-{channel}``, ``:sha-<short>``
    * ``release``, prerelease version -> ``:vX.Y.Z-beta.N``, ``:sha-<short>``
    * ``release`` with ``move_latest`` False -> no ``:latest``.

    Args:
        registry_bases: Registry base URLs from
            :func:`hyperi_ci.container.registry.resolve_registry_bases`
            (always ``["ghcr.io/<org>"]``).
        image_name: Image name (typically the repo name, e.g. ``dfe-loader``).
        version: Semantic version with no leading ``v``
            (e.g. ``"1.13.5"``).
        sha: Short git SHA.
        channel: Release channel (``alpha`` | ``beta`` | ``release``).
        mode: Push mode -- ``release`` | ``dev`` | ``validate``.
        branch_slug: Docker-tag-safe branch slug for dev mode
            (:func:`hyperi_ci.release_mode.dev_branch_slug`). Empty ->
            the dev image gets a ``dev-sha-<short>`` tag only.
        move_latest: False when a newer stable release owns ``:latest``
            (:func:`hyperi_ci.common.holds_latest`).

    Returns:
        Flat list of fully-qualified image tags. Empty for ``validate``.

    """
    if mode == "validate":
        return []

    if mode == "dev":
        if branch_slug:
            suffixes = [
                f"branch-{branch_slug}",
                f"branch-{branch_slug}-sha-{sha}",
            ]
        else:
            suffixes = [f"dev-sha-{sha}"]
    else:
        suffixes = _tag_suffixes(
            version=version, sha=sha, channel=channel, move_latest=move_latest
        )

    tags: list[str] = []
    for base in registry_bases:
        prefix = f"{base}/{image_name}"
        for suffix in suffixes:
            tags.append(f"{prefix}:{suffix}")
    return tags


def _tag_suffixes(
    *, version: str, sha: str, channel: str, move_latest: bool = True
) -> list[str]:
    # A prerelease version discriminates itself: `latest` belongs to the stable
    # sequence, and a channel suffix would render `v1.2.0-beta.1-beta`.
    if is_prerelease_version(version):
        return [f"v{version}", f"sha-{sha}"]
    if channel == "release":
        if not move_latest:
            return [f"v{version}", f"sha-{sha}"]
        return [f"v{version}", "latest", f"sha-{sha}"]
    return [f"v{version}-{channel}", f"sha-{sha}"]
