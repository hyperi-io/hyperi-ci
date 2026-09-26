# Project:   HyperI CI
# File:      src/hyperi_ci/container/build.py
# Purpose:   Docker buildx build and push execution
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Execute docker buildx build with optional multi-registry push."""

import string
import subprocess
from collections.abc import Mapping
from pathlib import Path

from hyperi_ci.common import error, info, success
from hyperi_ci.release_branches import is_prerelease_version

_BUILD_ARG_PLACEHOLDERS = ("version", "sha")


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


def build_and_push(
    *,
    dockerfile_path: Path,
    context: str = ".",
    tags: list[str],
    platforms: list[str],
    labels: dict[str, str],
    build_args: dict[str, str] | None = None,
    push: bool = True,
) -> int:
    """Build a container image with docker buildx and optionally push.

    When ``push`` is False the image is built but discarded (no
    ``--load``/``--push``). Multi-platform builds cannot ``--load`` into
    the local daemon, so the validate-on-main path relies on buildx's
    "build and discard" default -- every layer still compiles and every
    ``COPY`` / ``RUN`` is still exercised, but nothing leaves the
    runner.

    Args:
        dockerfile_path: Path to the Dockerfile.
        context: Docker build context directory.
        tags: List of full image tags spanning all target registries
            (e.g. ``["ghcr.io/hyperi-io/app:v1.0.0", "ghcr.io/hyperi-io/app:latest"]``).
        platforms: Target platforms (e.g. ``["linux/amd64", "linux/arm64"]``).
        labels: OCI labels dict.
        build_args: Additional ``--build-arg key=value`` pairs, already
            rendered by :func:`render_build_args`.
        push: When True, push to all tagged registries. When False, build
            but discard (validation only).

    Returns:
        Exit code (0 = success).

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

    for key, value in sorted(labels.items()):
        cmd.extend(["--label", f"{key}={value}"])

    if build_args:
        for key, value in sorted(build_args.items()):
            cmd.extend(["--build-arg", f"{key}={value}"])

    if push:
        cmd.append("--push")
    # No --load / --push: multi-arch builds cannot load into the local
    # daemon (it only handles one platform at a time). The default
    # "build and discard" still validates the full Dockerfile.

    cmd.append(context)

    action = "Pushing" if push else "Validating (no push)"
    info(f"{action}: {', '.join(tags) if tags else '<no tags>'}")
    info(f"Platforms: {', '.join(platforms)}")

    result = subprocess.run(cmd, capture_output=False)

    if result.returncode != 0:
        error("docker buildx build failed")
        return result.returncode

    action = "pushed" if push else "validated"
    if tags:
        success(f"Built and {action}: {tags[0]}")
    else:
        success(f"Built and {action}")
    return 0


def resolve_tags(
    *,
    registry_bases: list[str],
    image_name: str,
    version: str,
    sha: str,
    channel: str = "release",
    mode: str = "release",
    branch_slug: str = "",
) -> list[str]:
    """Generate image tags spanning all configured registries.

    Tag matrix per registry base, by push mode
    (:mod:`hyperi_ci.release_mode`):

    * ``validate``                 → no tags (build-and-discard)
    * ``dev``                      → ``:branch-<slug>`` (mutable pointer) +
      ``:branch-<slug>-sha-<short>`` (immutable pin) -- the branch
      dev-image artifact class (plan decision 3). NEVER a version tag,
      NEVER ``latest``, and NEVER a bare ``sha-<short>``: that namespace
      belongs to the GA publish, and the distinct ``branch-*`` /
      ``dev-sha-*`` prefixes are what lets the scheduled GHCR pruner
      (``_ghcr-prune.yml``) glob dev tags without ever touching GA pins.
    * ``release``, release channel → ``:vX.Y.Z``, ``:latest``, ``:sha-<short>``
    * ``release``, pre-GA channel  → ``:vX.Y.Z-{channel}``, ``:sha-<short>``
    * ``release``, prerelease version → ``:vX.Y.Z-beta.N``, ``:sha-<short>``
      -- a version off a prerelease branch never moves ``latest``.

    The SHA tag is included on every pushed build to give consumers an
    immutable-by-content pin alongside the human-readable tag.

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
            (:func:`hyperi_ci.release_mode.dev_branch_slug`). Empty →
            the dev image gets a ``dev-sha-<short>`` tag only.

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
        suffixes = _tag_suffixes(version=version, sha=sha, channel=channel)

    tags: list[str] = []
    for base in registry_bases:
        prefix = f"{base}/{image_name}"
        for suffix in suffixes:
            tags.append(f"{prefix}:{suffix}")
    return tags


def _tag_suffixes(*, version: str, sha: str, channel: str) -> list[str]:
    # A prerelease version discriminates itself: `latest` belongs to the stable
    # sequence, and a channel suffix would render `v1.2.0-beta.1-beta`.
    if is_prerelease_version(version):
        return [f"v{version}", f"sha-{sha}"]
    if channel == "release":
        return [f"v{version}", "latest", f"sha-{sha}"]
    return [f"v{version}-{channel}", f"sha-{sha}"]
