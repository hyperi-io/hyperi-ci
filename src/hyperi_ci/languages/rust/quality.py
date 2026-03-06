# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/quality.py
# Purpose:   Rust quality checks (fmt, clippy, audit, deny)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust quality checks handler.

Orchestrates: cargo fmt --check, cargo clippy, cargo audit, cargo deny.
Each tool's mode (blocking/warn/disabled) is configurable via
.hyperi-ci.yaml quality.rust section.
"""

from __future__ import annotations

import shutil
import subprocess

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig


def _get_tool_mode(tool: str, config: CIConfig) -> str:
    """Get quality tool mode: blocking, warn, or disabled."""
    return str(config.get(f"quality.rust.{tool}", "blocking"))


def _run_tool(tool_name: str, cmd: list[str], mode: str) -> bool:
    """Run a quality tool. Returns True if pipeline should continue."""
    if mode == "disabled":
        info(f"  {tool_name}: disabled")
        return True

    if not shutil.which(cmd[0]):
        if mode == "blocking":
            error(f"  {tool_name}: not installed (required)")
            return False
        warn(f"  {tool_name}: not installed (skipping)")
        return True

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        success(f"  {tool_name}: passed")
        return True

    if mode == "warn":
        warn(f"  {tool_name}: issues found (non-blocking)")
        if result.stdout:
            print(result.stdout)
        return True

    error(f"  {tool_name}: failed")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return False


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Rust quality checks.

    Args:
        config: Merged CI configuration.
        extra_env: Additional env vars (RUST_FEATURES).

    Returns:
        Exit code (0 = success).
    """
    info("Running Rust quality checks...")
    had_failure = False

    # cargo fmt --check
    mode = _get_tool_mode("fmt", config)
    if not _run_tool("cargo fmt", ["cargo", "fmt", "--check"], mode):
        had_failure = True

    # cargo clippy
    mode = _get_tool_mode("clippy", config)
    features = (extra_env or {}).get("RUST_FEATURES", "all")
    clippy_cmd = ["cargo", "clippy", "--all-targets"]
    if features == "all":
        clippy_cmd.append("--all-features")
    elif features != "default":
        for feature_set in features.split("|"):
            clippy_cmd.extend(["--features", feature_set.strip()])
    clippy_cmd.extend(["--", "-D", "warnings"])
    if not _run_tool("cargo clippy", clippy_cmd, mode):
        had_failure = True

    # cargo audit
    mode = _get_tool_mode("audit", config)
    if not _run_tool("cargo audit", ["cargo", "audit"], mode):
        had_failure = True

    # cargo deny
    mode = _get_tool_mode("deny", config)
    if not _run_tool("cargo deny", ["cargo", "deny", "check"], mode):
        had_failure = True

    return 1 if had_failure else 0
