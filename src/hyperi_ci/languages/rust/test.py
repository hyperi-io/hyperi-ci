# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/test.py
# Purpose:   Rust test runner (cargo nextest with tiered execution)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust test handler.

Runs cargo nextest (preferred) or cargo test with optional tiered execution.
Supports coverage via cargo-tarpaulin or cargo-llvm-cov.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig

_RESULTS_DIR = Path("test-results")


def _has_nextest() -> bool:
    """Check if cargo-nextest is installed."""
    return shutil.which("cargo-nextest") is not None


def _build_test_cmd(features: str, tier: str | None = None) -> list[str]:
    """Build the cargo test command."""
    use_nextest = _has_nextest()
    cmd = ["cargo"]

    if use_nextest:
        cmd.append("nextest")
        cmd.append("run")
    else:
        cmd.append("test")

    if features == "all":
        cmd.append("--all-features")
    elif features != "default":
        cmd.extend(["--features", features])

    if tier == "unit":
        cmd.append("--lib")
    elif tier == "integration":
        cmd.extend(["--test", "*"])
    elif tier == "e2e":
        cmd.extend(["--test", "e2e*"])

    return cmd


def _run_coverage(features: str) -> int:
    """Run tests with coverage using tarpaulin or llvm-cov.

    Returns exit code (0 = success).
    """
    _RESULTS_DIR.mkdir(exist_ok=True)

    if shutil.which("cargo-tarpaulin"):
        cmd = [
            "cargo",
            "tarpaulin",
            "--out",
            "Lcov",
            "--out",
            "Html",
            "--output-dir",
            str(_RESULTS_DIR),
        ]
        if features == "all":
            cmd.append("--all-features")
        elif features != "default":
            cmd.extend(["--features", features])

        info("  Running tests with cargo-tarpaulin for coverage...")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            error("Rust coverage tests failed")
            return result.returncode
        info(f"  Coverage report: {_RESULTS_DIR}/tarpaulin-report.html")
        return 0

    if shutil.which("cargo-llvm-cov"):
        lcov_path = _RESULTS_DIR / "lcov.info"
        html_dir = _RESULTS_DIR / "coverage-html"
        cmd = ["cargo", "llvm-cov", "--lcov", "--output-path", str(lcov_path)]
        if features == "all":
            cmd.append("--all-features")
        elif features != "default":
            cmd.extend(["--features", features])

        info("  Running tests with cargo-llvm-cov for coverage...")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            error("Rust coverage tests failed")
            return result.returncode

        # Generate HTML report
        subprocess.run(
            [
                "cargo",
                "llvm-cov",
                "report",
                "--html",
                "--output-dir",
                str(html_dir),
            ],
            check=False,
        )
        info(f"  Coverage report: {html_dir}")
        return 0

    warn("  No coverage tool found (cargo-tarpaulin or cargo-llvm-cov)")
    warn("  Running tests without coverage")
    return -1


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Rust tests.

    Args:
        config: Merged CI configuration.
        extra_env: Additional env vars (RUST_FEATURES).

    Returns:
        Exit code (0 = success).
    """
    info("Running Rust tests...")
    features = (extra_env or {}).get("RUST_FEATURES", "all")
    tier = config.get("test.rust.tier", "all")

    # Try coverage first if enabled
    if config.get("test.coverage", True) and tier == "all":
        rc = _run_coverage(features)
        if rc >= 0:
            if rc == 0:
                success("Rust tests passed (with coverage)")
            return rc

    # Standard test execution (no coverage tool, or tiered)
    if tier == "all":
        cmd = _build_test_cmd(features)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            error("Rust tests failed")
            return result.returncode
        success("Rust tests passed")
        return 0

    # Tiered execution
    for t in ("unit", "integration", "e2e"):
        if tier != "all" and tier != t:
            continue
        cmd = _build_test_cmd(features, tier=t)
        info(f"  Running {t} tests...")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            error(f"  {t} tests failed")
            return result.returncode
        success(f"  {t} tests passed")

    return 0
