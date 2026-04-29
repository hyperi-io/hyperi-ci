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


def _split_feature_sets(features: str) -> list[str]:
    """Split pipe-separated feature sets into individual sets."""
    if features in ("all", "default"):
        return [features]
    return [f.strip() for f in features.split("|") if f.strip()]


def _has_nextest() -> bool:
    """Check if cargo-nextest is installed."""
    return shutil.which("cargo-nextest") is not None


def _build_test_cmd(features: str, tier: str | None = None) -> list[str]:
    """Build the cargo test command.

    Integration and e2e tests default to single-threaded execution to avoid
    port conflicts from parallel test processes binding the same addresses.
    """
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

    # Limit integration/e2e tests to 1 thread to avoid port conflicts
    if tier in ("integration", "e2e"):
        if use_nextest:
            cmd.extend(["--jobs", "1"])
        else:
            cmd.extend(["--", "--test-threads=1"])

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
    feature_sets = _split_feature_sets(features)

    for feature_set in feature_sets:
        label = f" ({feature_set})" if len(feature_sets) > 1 else ""

        # Try coverage first if enabled (only for first feature set)
        if config.get("test.coverage", True) and tier == "all":
            rc = _run_coverage(feature_set)
            if rc >= 0:
                if rc == 0:
                    success(f"Rust tests passed{label} (with coverage)")
                else:
                    return rc
                continue

        # Standard test execution (no coverage tool, or tiered)
        if tier == "all":
            cmd = _build_test_cmd(feature_set)
            result = subprocess.run(cmd)
            if result.returncode != 0:
                error(f"Rust tests failed{label}")
                return result.returncode
            success(f"Rust tests passed{label}")
            continue

        # Tiered execution
        for t in ("unit", "integration", "e2e"):
            if tier != "all" and tier != t:
                continue
            cmd = _build_test_cmd(feature_set, tier=t)
            info(f"  Running {t} tests{label}...")
            result = subprocess.run(cmd)
            if result.returncode != 0:
                error(f"  {t} tests failed{label}")
                return result.returncode
            success(f"  {t} tests passed{label}")

    return 0
