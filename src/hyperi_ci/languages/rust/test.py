# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/test.py
# Purpose:   Rust test runner (cargo nextest with tiered execution)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust test handler.

Runs cargo nextest (preferred) or cargo test with optional tiered execution.
"""

from __future__ import annotations

import shutil
import subprocess

from hyperi_ci.common import error, info, success
from hyperi_ci.config import CIConfig


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
