# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/test.py
# Purpose:   Golang test runner
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang test handler."""

from __future__ import annotations

import subprocess

from hyperi_ci.common import error, info, success
from hyperi_ci.config import CIConfig


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Golang tests."""
    info("Running Golang tests...")

    cmd = ["go", "test"]
    args = list(config.get("test.golang.args", ["-v"]))
    cmd.extend(args)

    if config.get("test.golang.race", True):
        cmd.append("-race")

    cmd.append("./...")

    result = subprocess.run(cmd)
    if result.returncode != 0:
        error("Golang tests failed")
        return result.returncode

    success("Golang tests passed")
    return 0
