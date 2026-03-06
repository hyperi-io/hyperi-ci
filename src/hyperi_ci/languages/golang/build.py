# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/build.py
# Purpose:   Golang build handler with cross-compilation
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang build handler."""

from __future__ import annotations

import os
import subprocess

from hyperi_ci.common import error, group, info, success
from hyperi_ci.config import CIConfig


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Golang build."""
    info("Building Golang project...")

    targets = config.get("build.golang.targets", ["linux/amd64"])
    cgo = config.get("build.golang.cgo", False)

    for target in targets:
        parts = target.split("/")
        if len(parts) != 2:
            error(f"Invalid Go target: {target}")
            return 1

        goos, goarch = parts
        with group(f"Build: {goos}/{goarch}"):
            env = {
                **os.environ,
                "GOOS": goos,
                "GOARCH": goarch,
                "CGO_ENABLED": "1" if cgo else "0",
            }
            result = subprocess.run(
                ["go", "build", "-o", f"dist/{goos}-{goarch}/", "./..."],
                env=env,
            )
            if result.returncode != 0:
                error(f"Build failed for {goos}/{goarch}")
                return result.returncode
            success(f"Built: {goos}/{goarch}")

    return 0
