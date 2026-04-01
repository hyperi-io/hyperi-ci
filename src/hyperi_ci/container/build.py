# Project:   HyperI CI
# File:      src/hyperi_ci/container/build.py
# Purpose:   Docker buildx build and push execution
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Execute docker buildx build and push container images."""

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

    Args:
        dockerfile_path: Path to the Dockerfile.
        context: Docker build context directory.
        tags: List of full image tags (e.g. ["ghcr.io/hyperi-io/app:v1.0.0"]).
        platforms: Target platforms (e.g. ["linux/amd64", "linux/arm64"]).
        labels: OCI labels dict.
        build_args: Additional --build-arg key=value pairs.
        push: Whether to push after building.

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
    else:
        cmd.append("--load")

    cmd.append(context)

    info(f"Building: {', '.join(tags)}")
    info(f"Platforms: {', '.join(platforms)}")

    result = subprocess.run(cmd, capture_output=False)

    if result.returncode != 0:
        error("Docker buildx build failed")
        return result.returncode

    action = "pushed" if push else "loaded"
    success(f"Built and {action}: {tags[0]}")
    return 0


def resolve_tags(
    *,
    registry: str,
    image_name: str,
    version: str,
    sha: str,
    channel: str = "release",
    is_push_to_main: bool = False,
) -> list[str]:
    """Generate image tags based on context.

    Args:
        registry: Registry URL (e.g. "ghcr.io/hyperi-io").
        image_name: Image name (e.g. "dfe-loader").
        version: Semantic version (e.g. "1.13.5").
        sha: Short git SHA.
        channel: Publish channel (spike/alpha/beta/release).
        is_push_to_main: True if this is a push-to-main build (not dispatch).

    Returns:
        List of full image tags.
    """
    base = f"{registry}/{image_name}"

    if is_push_to_main:
        return [f"{base}:sha-{sha}"]

    if channel == "release":
        return [f"{base}:v{version}", f"{base}:latest"]

    return [f"{base}:v{version}-{channel}"]
