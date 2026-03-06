# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/quality.py
# Purpose:   TypeScript quality checks (eslint, prettier, tsc, audit)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript quality checks handler."""

from __future__ import annotations

import subprocess

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig


def _detect_package_manager() -> str:
    """Detect which package manager the project uses."""
    from pathlib import Path

    if Path("pnpm-lock.yaml").exists():
        return "pnpm"
    if Path("yarn.lock").exists():
        return "yarn"
    return "npm"


def _get_tool_mode(tool: str, config: CIConfig) -> str:
    return str(config.get(f"quality.typescript.{tool}", "blocking"))


def _run_tool(tool_name: str, cmd: list[str], mode: str) -> bool:
    if mode == "disabled":
        info(f"  {tool_name}: disabled")
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
    """Run TypeScript quality checks."""
    info("Running TypeScript quality checks...")
    pm = _detect_package_manager()
    had_failure = False

    mode = _get_tool_mode("eslint", config)
    if not _run_tool("eslint", [pm, "run", "lint"], mode):
        had_failure = True

    mode = _get_tool_mode("prettier", config)
    if not _run_tool("prettier", [pm, "run", "format", "--check"], mode):
        had_failure = True

    mode = _get_tool_mode("tsc", config)
    if not _run_tool("tsc", [pm, "run", "typecheck"], mode):
        had_failure = True

    mode = _get_tool_mode("audit", config)
    audit_level = config.get("quality.typescript.audit_level", "moderate")
    if not _run_tool("audit", [pm, "audit", f"--audit-level={audit_level}"], mode):
        had_failure = True

    return 1 if had_failure else 0
