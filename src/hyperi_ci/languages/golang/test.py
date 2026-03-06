# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/test.py
# Purpose:   Golang test runner with coverage support
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang test handler.

Runs go test with optional race detection and coverage reporting.
Coverage output goes to test-results/ for artifact upload.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success
from hyperi_ci.config import CIConfig

_RESULTS_DIR = Path("test-results")


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Golang tests."""
    info("Running Golang tests...")

    cmd = ["go", "test"]
    args = list(config.get("test.golang.args", ["-v"]))
    cmd.extend(args)

    if config.get("test.golang.race", True):
        cmd.append("-race")

    # Coverage reporting
    if config.get("test.coverage", True):
        _RESULTS_DIR.mkdir(exist_ok=True)
        coverage_file = _RESULTS_DIR / "coverage.out"
        cmd.extend(["-coverprofile", str(coverage_file), "-covermode=atomic"])

    cmd.append("./...")

    result = subprocess.run(cmd)
    if result.returncode != 0:
        error("Golang tests failed")
        return result.returncode

    # Generate HTML coverage report if coverage was collected
    coverage_file = _RESULTS_DIR / "coverage.out"
    if coverage_file.exists():
        html_file = _RESULTS_DIR / "coverage.html"
        subprocess.run(
            ["go", "tool", "cover", f"-html={coverage_file}", f"-o={html_file}"],
            check=False,
        )
        info(f"  Coverage report: {html_file}")

    success("Golang tests passed")
    return 0
