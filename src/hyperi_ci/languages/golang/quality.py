# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/quality.py
# Purpose:   Golang quality checks (gofmt, govet, golangci-lint, gosec)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang quality checks handler."""

from __future__ import annotations

import shutil
import subprocess

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig


def _get_tool_mode(tool: str, config: CIConfig) -> str:
    return str(config.get(f"quality.golang.{tool}", "blocking"))


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
    """Run Golang quality checks."""
    info("Running Golang quality checks...")
    had_failure = False

    mode = _get_tool_mode("gofmt", config)
    if not _run_tool("gofmt", ["gofmt", "-l", "."], mode):
        had_failure = True

    mode = _get_tool_mode("govet", config)
    if not _run_tool("go vet", ["go", "vet", "./..."], mode):
        had_failure = True

    mode = _get_tool_mode("golangci_lint", config)
    if not _run_tool(
        "golangci-lint", ["golangci-lint", "run", "--timeout", "5m"], mode
    ):
        had_failure = True

    mode = _get_tool_mode("gosec", config)
    if not _run_tool("gosec", ["gosec", "-quiet", "./..."], mode):
        had_failure = True

    mode = _get_tool_mode("govulncheck", config)
    if not _run_tool("govulncheck", ["govulncheck", "./..."], mode):
        had_failure = True

    # Semgrep SAST scanning
    mode = _get_tool_mode("semgrep", config)
    semgrep_cmd = ["semgrep", "scan", "--config", "auto", "--error", "--quiet"]
    if not _run_tool("semgrep", semgrep_cmd, mode, use_uvx=True):
        had_failure = True

    return 1 if had_failure else 0
