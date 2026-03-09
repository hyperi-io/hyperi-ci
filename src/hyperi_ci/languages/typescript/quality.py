# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/quality.py
# Purpose:   TypeScript quality checks (eslint, prettier, tsc, audit)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript quality checks handler."""

from __future__ import annotations

import shutil
import subprocess

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig


def _find_npm_script(
    candidates: list[str],
    pm: str,
) -> str | None:
    """Find the first matching npm script from candidates.

    Args:
        candidates: Script names to try in order.
        pm: Package manager command.

    Returns:
        First matching script name, or None.
    """
    import json
    from pathlib import Path

    pkg = Path("package.json")
    if not pkg.exists():
        return None

    try:
        data = json.loads(pkg.read_text())
        scripts = data.get("scripts", {})
        for name in candidates:
            if name in scripts:
                return name
    except (json.JSONDecodeError, KeyError):
        pass
    return None


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
    if mode == "disabled":
        info(f"  {tool_name}: disabled")
        return True

    resolved = _resolve_tool_cmd(cmd, use_uvx=use_uvx)
    if use_uvx and resolved == cmd and not shutil.which(cmd[0]):
        if mode == "blocking":
            error(f"  {tool_name}: not installed (required)")
            return False
        warn(f"  {tool_name}: not installed (skipping)")
        return True

    result = subprocess.run(resolved, capture_output=True, text=True)
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
    tsc_script = _find_npm_script(
        ["typecheck", "check-types"],
        pm,
    )
    if tsc_script:
        tsc_cmd = [pm, "run", tsc_script]
    else:
        tsc_cmd = ["npx", "tsc", "--noEmit"]
    if not _run_tool("tsc", tsc_cmd, mode):
        had_failure = True

    mode = _get_tool_mode("audit", config)
    audit_level = config.get("quality.typescript.audit_level", "moderate")
    if not _run_tool("audit", [pm, "audit", f"--audit-level={audit_level}"], mode):
        had_failure = True

    # Semgrep SAST scanning
    mode = _get_tool_mode("semgrep", config)
    semgrep_cmd = ["semgrep", "scan", "--config", "auto", "--error", "--quiet"]
    if not _run_tool("semgrep", semgrep_cmd, mode, use_uvx=True):
        had_failure = True

    return 1 if had_failure else 0
