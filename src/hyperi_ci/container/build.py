# Project:   HyperI CI
# File:      src/hyperi_ci/container/build.py
# Purpose:   Docker buildx build and push execution
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Execute docker buildx build with optional multi-registry push."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success


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
    "build and discard" default — every layer still compiles and every
    ``COPY`` / ``RUN`` is still exercised, but nothing leaves the
    runner.

    Args:
        dockerfile_path: Path to the Dockerfile.
        context: Docker build context directory.
        tags: List of full image tags spanning all target registries
            (e.g. ``["ghcr.io/hyperi-io/app:v1.0.0", "ghcr.io/hyperi-io/app:latest"]``).
        platforms: Target platforms (e.g. ``["linux/amd64", "linux/arm64"]``).
        labels: OCI labels dict.
        build_args: Additional ``--build-arg key=value`` pairs.
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
    is_push_to_main: bool = False,
) -> list[str]:
    """Generate image tags spanning all configured registries.

    Tag matrix per registry base:

    * push-to-main (validate-only) → ``:sha-<short>``  (no tags actually
      land in any registry — this list is used only when buildx pushes)
    * release channel              → ``:vX.Y.Z``, ``:latest``, ``:sha-<short>``
    * pre-GA channel               → ``:vX.Y.Z-{channel}``, ``:sha-<short>``

    The SHA tag is included on every published build to give consumers
    an immutable-by-content pin alongside the human-readable version.

    Args:
        registry_bases: Registry base URLs from
            :func:`hyperi_ci.container.registry.resolve_registry_bases`
            (always ``["ghcr.io/<org>"]`` since JFrog was removed in v2.1.4).
        image_name: Image name (typically the repo name, e.g. ``dfe-loader``).
        version: Semantic version with no leading ``v``
            (e.g. ``"1.13.5"``).
        sha: Short git SHA.
        channel: Publish channel (``spike`` | ``alpha`` | ``beta`` |
            ``release``).
        is_push_to_main: True for push-to-main validate runs.

    Returns:
        Flat list of fully-qualified image tags. Empty list when
        ``is_push_to_main`` is True and no tags should be applied
        (validate-only does not produce registry tags).

    """
    if is_push_to_main:
        return []

    suffixes = _tag_suffixes(version=version, sha=sha, channel=channel)
    tags: list[str] = []
    for base in registry_bases:
        prefix = f"{base}/{image_name}"
        for suffix in suffixes:
            tags.append(f"{prefix}:{suffix}")
    return tags


def _tag_suffixes(*, version: str, sha: str, channel: str) -> list[str]:
    if channel == "release":
        return [f"v{version}", "latest", f"sha-{sha}"]
    return [f"v{version}-{channel}", f"sha-{sha}"]
