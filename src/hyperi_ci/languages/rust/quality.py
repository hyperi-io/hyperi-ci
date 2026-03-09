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
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig


def _get_tool_mode(tool: str, config: CIConfig) -> str:
    """Get quality tool mode: blocking, warn, or disabled."""
    return str(config.get(f"quality.rust.{tool}", "blocking"))


def _resolve_tool_cmd(cmd: list[str], use_uvx: bool = False) -> list[str]:
    """Resolve tool command, using uvx for standalone tools not on PATH."""
    if shutil.which(cmd[0]):
        return cmd
    if use_uvx and shutil.which("uvx"):
        return ["uvx", *cmd]
    return cmd


def _run_tool(
    tool_name: str,
    cmd: list[str],
    mode: str,
    use_uvx: bool = False,
) -> bool:
    """Run a quality tool. Returns True if pipeline should continue."""
    if mode == "disabled":
        info(f"  {tool_name}: disabled")
        return True

    resolved = _resolve_tool_cmd(cmd, use_uvx=use_uvx)
    if resolved == cmd and not shutil.which(cmd[0]):
        if mode == "blocking":
            error(f"  {tool_name}: not installed (required)")
            return False
        warn(f"  {tool_name}: not installed (skipping)")
        return True

    result = subprocess.run(resolved, capture_output=True, text=True)

    if result.returncode == 0:
        success(f"  {tool_name}: passed")
        return True

    # Transient failures (e.g. cargo audit "error loading advisory database")
    # should not block CI — treat as warning regardless of mode
    combined = (result.stdout or "") + (result.stderr or "")
    if "error loading advisory database" in combined.lower():
        warn(f"  {tool_name}: advisory database unavailable (skipping)")
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
    clippy_cmd.extend(["--", "-D", "warnings", "-D", "clippy::dbg_macro"])
    if not _run_tool("cargo clippy", clippy_cmd, mode):
        had_failure = True

    # cargo audit
    mode = _get_tool_mode("audit", config)
    if not _run_tool("cargo audit", ["cargo", "audit"], mode):
        had_failure = True

    # cargo deny (requires deny.toml — useless without project-specific config)
    mode = _get_tool_mode("deny", config)
    if not Path("deny.toml").exists():
        info("  cargo deny: skipped (no deny.toml found)")
    elif not _run_tool("cargo deny", ["cargo", "deny", "check"], mode):
        had_failure = True

    # Semgrep SAST scanning
    mode = _get_tool_mode("semgrep", config)
    semgrep_cmd = ["semgrep", "scan", "--config", "auto", "--error", "--quiet"]
    if not _run_tool("semgrep", semgrep_cmd, mode, use_uvx=True):
        had_failure = True

    return 1 if had_failure else 0
