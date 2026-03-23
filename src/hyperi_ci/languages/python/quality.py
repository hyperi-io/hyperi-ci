# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/quality.py
# Purpose:   Python quality checks (ruff, ty, semgrep, bandit, pip-audit, vulture)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python quality checks handler.

Orchestrates quality tools: ruff (lint + format + docstrings), ty, semgrep,
bandit, pip-audit, vulture. Each tool's mode (blocking/warn/disabled) is
configurable via .hyperi-ci.yaml quality.python section.

Docstring coverage uses ruff D rules (pydocstyle) instead of interrogate,
which is unmaintained and pulls in the vulnerable 'py' package.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, get_exclude_dirs, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import get_test_ignore, get_test_paths

_DEFAULT_PYTHON_TEST_IGNORE = [
    "S101",
    "S104",
    "S105",
    "S108",
    "S311",
    "T201",
    "PT003",
    "PT006",
    "PT011",
    "PT012",
    "PT017",
    "PT018",
    "PT001",
    "PT021",
    "S106",
    "N803",
    "RUF003",
    "RUF015",
    "RUF043",
]


def _get_tool_mode(tool: str, config: CIConfig) -> str:
    """Get quality tool mode: blocking, warn, or disabled."""
    return str(config.get(f"quality.python.{tool}", "blocking"))


def _build_exclude_args(tool: str, excludes: list[str]) -> list[str]:
    """Build exclusion arguments for a quality tool."""
    if tool == "ruff":
        return [f"--exclude={','.join(excludes)}"] if excludes else []
    if tool == "bandit":
        return [f"--exclude={','.join(excludes)}"] if excludes else []
    if tool == "vulture":
        return [f"--exclude={','.join(excludes)}"] if excludes else []
    return []


def _resolve_tool_cmd(cmd: list[str], use_uvx: bool = False) -> list[str]:
    """Resolve tool command, using uv run if tool isn't on PATH.

    When hyperi-ci runs via uvx, project tools (ruff, pytest, etc.)
    live in the project's .venv, not on PATH. Prefix with 'uv run'
    to execute within the project's virtual environment.

    Args:
        cmd: Command and arguments.
        use_uvx: If True, use 'uvx' instead of 'uv run' for tools
            that are standalone (not project deps).
    """
    if shutil.which(cmd[0]):
        return cmd
    if shutil.which("uv"):
        if use_uvx:
            return ["uvx", *cmd]
        return ["uv", "run", *cmd]
    return cmd


def _run_tool(
    tool_name: str,
    cmd: list[str],
    mode: str,
    use_uvx: bool = False,
) -> bool:
    """Run a quality tool and handle its result based on mode.

    Returns True if pipeline should continue, False if blocking failure.
    """
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
    """Run Python quality checks.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables (unused for Python).

    Returns:
        Exit code (0 = success).
    """
    info("Running Python quality checks...")
    excludes = get_exclude_dirs(config._raw)
    had_failure = False

    # Ruff lint — two-pass: production (strict) + test (relaxed)
    mode = _get_tool_mode("ruff", config)
    exclude_args = _build_exclude_args("ruff", excludes)
    # Use GitHub-native annotations in CI for inline PR feedback
    output_fmt = ["--output-format=github"] if os.environ.get("GITHUB_ACTIONS") else []

    test_paths = get_test_paths(config)
    test_ignore = get_test_ignore("python", config, _DEFAULT_PYTHON_TEST_IGNORE)

    # Production pass — exclude test dirs, full rules
    prod_exclude = exclude_args + [f"--exclude={p}" for p in test_paths]
    if not _run_tool(
        "ruff check (src)", ["ruff", "check", "."] + output_fmt + prod_exclude, mode
    ):
        had_failure = True

    # Test pass — relaxed rules, same mode
    if test_paths and test_ignore:
        ignore_flag = [f"--extend-ignore={','.join(test_ignore)}"]
        for tp in test_paths:
            if not _run_tool(
                f"ruff check ({tp})",
                ["ruff", "check", tp] + output_fmt + ignore_flag + exclude_args,
                mode,
            ):
                had_failure = True

    # Ruff format — single pass (format is rule-agnostic, no split needed)
    if not _run_tool(
        "ruff format", ["ruff", "format", "--check", "."] + exclude_args, mode
    ):
        had_failure = True

    # Type checking (ty from Astral, or pyright as fallback)
    ty_mode = _get_tool_mode("ty", config)
    pyright_mode = _get_tool_mode("pyright", config)
    if ty_mode != "disabled":
        if not _run_tool("ty", ["ty", "check"], ty_mode):
            had_failure = True
    elif pyright_mode != "disabled":
        if not _run_tool("pyright", ["pyright"], pyright_mode):
            had_failure = True

    # Semgrep SAST scanning
    mode = _get_tool_mode("semgrep", config)
    semgrep_cmd = ["semgrep", "scan", "--config", "auto", "--error", "--quiet"]
    if excludes:
        for exc in excludes:
            semgrep_cmd.extend(["--exclude", exc])
    if not _run_tool("semgrep", semgrep_cmd, mode, use_uvx=True):
        had_failure = True

    # Bandit security scanning
    mode = _get_tool_mode("bandit", config)
    bandit_cmd = ["bandit", "-r", "src/", "-ll"]
    if Path("pyproject.toml").exists():
        bandit_cmd.extend(["-c", "pyproject.toml"])
    if config.get("quality.python.bandit_exclude_tests", True):
        bandit_cmd.extend(["--exclude", "tests/"])
    bandit_cmd.extend(_build_exclude_args("bandit", excludes))
    if not _run_tool("bandit", bandit_cmd, mode, use_uvx=True):
        had_failure = True

    # pip-audit vulnerability scanning
    mode = _get_tool_mode("pip_audit", config)
    if not _run_tool("pip-audit", ["pip-audit"], mode, use_uvx=True):
        had_failure = True

    # Docstring coverage via ruff D rules (replaces interrogate)
    mode = _get_tool_mode("ruff_docstrings", config)
    ruff_doc_cmd = ["ruff", "check", "--select", "D", "src/"] + _build_exclude_args(
        "ruff", excludes
    )
    if not _run_tool("ruff docstrings", ruff_doc_cmd, mode):
        had_failure = True

    # Vulture dead code detection
    mode = _get_tool_mode("vulture", config)
    vulture_cmd = ["vulture", "src/"] + _build_exclude_args("vulture", excludes)
    if not _run_tool("vulture", vulture_cmd, mode, use_uvx=True):
        had_failure = True

    return 1 if had_failure else 0
