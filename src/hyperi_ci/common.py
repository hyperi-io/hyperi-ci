# Project:   HyperI CI
# File:      src/hyperi_ci/common.py
# Purpose:   Shared utilities for CI scripts (output, subprocess, exclusions)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared utilities for HyperI CI.

Uses hyperi-pylib logger for structured output with automatic environment
detection (GitHub Actions workflow commands, Solarized terminal, plain CI).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hyperi_pylib.logger import logger

# Initialise logger for CI use (auto-detects GH Actions, CI, terminal)
from hyperi_pylib.logger import setup as _setup_logger

_setup_logger(ci_mode=None, mask_sensitive=True)


def is_ci() -> bool:
    """Detect if running in a CI/runner environment."""
    return any(
        os.environ.get(v)
        for v in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE")
    )


def is_github_actions() -> bool:
    """Detect if running in GitHub Actions specifically."""
    return bool(os.environ.get("GITHUB_ACTIONS"))


def is_interactive() -> bool:
    """Detect if running in an interactive terminal (supports colours)."""
    if not sys.stderr.isatty():
        return False
    term = os.environ.get("TERM", "")
    if term == "dumb" or not term:
        return False
    if is_ci():
        return False
    return True


def is_macos() -> bool:
    """Detect if running on macOS."""
    return sys.platform == "darwin"


def is_linux() -> bool:
    """Detect if running on Linux."""
    return sys.platform.startswith("linux")


def info(msg: str) -> None:
    """Info message — delegates to hyperi-pylib logger."""
    logger.info(msg)


def success(msg: str) -> None:
    """Success message — delegates to hyperi-pylib logger."""
    logger.success(msg)


def warn(msg: str) -> None:
    """Warning — delegates to hyperi-pylib logger."""
    logger.warning(msg)


def error(msg: str) -> None:
    """Error — delegates to hyperi-pylib logger."""
    logger.error(msg)


def fatal(msg: str) -> None:
    """Fatal error — log and exit with code 1."""
    logger.critical(msg)
    sys.exit(1)


@contextmanager
def group(title: str) -> Iterator[None]:
    """Collapsible group in GH Actions logs. No-op elsewhere."""
    if is_github_actions():
        print(f"::group::{title}")
    try:
        yield
    finally:
        if is_github_actions():
            print("::endgroup::")


def set_output(name: str, value: str) -> None:
    """Set a GH Actions step output parameter via GITHUB_OUTPUT file."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{name}={value}\n")


def set_env(name: str, value: str) -> None:
    """Set a GH Actions environment variable via GITHUB_ENV file."""
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a") as f:
            f.write(f"{name}={value}\n")


def mask(value: str) -> None:
    """Mask a value in GH Actions logs."""
    if is_github_actions():
        print(f"::add-mask::{value}")


def run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with consistent error handling.

    Args:
        cmd: Command as list of strings.
        check: Raise CalledProcessError on non-zero exit.
        capture: Capture stdout/stderr instead of passing through.
        cwd: Working directory.
        env: Additional env vars (merged with os.environ).

    Returns:
        CompletedProcess with text output.
    """
    run_env = None
    if env:
        run_env = {**os.environ, **env}

    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        cwd=cwd,
        env=run_env,
    )


def verify_publish(
    url: str,
    *,
    auth: tuple[str, str] | None = None,
    max_retries: int = 5,
    retry_delay: int = 10,
    label: str = "",
) -> bool:
    """Verify a published artifact is reachable via HTTP HEAD with retries.

    JFrog Artifactory has indexing lag — a just-published artifact may return
    404 for several seconds. This retries with delay to account for that.

    Args:
        url: Full URL to HEAD-check.
        auth: Optional (username, password) tuple for basic auth.
        max_retries: Maximum number of attempts.
        retry_delay: Seconds to wait between retries.
        label: Human-readable label for log messages.

    Returns:
        True if the artifact was found (HTTP 200), False otherwise.
    """
    import time

    display = label or url.rsplit("/", 1)[-1]

    for attempt in range(1, max_retries + 1):
        cmd = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--head"]
        if auth:
            cmd.extend(["-u", f"{auth[0]}:{auth[1]}"])
        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True)
        http_code = result.stdout.strip()

        if http_code == "200":
            success(f"  Verified: {display}")
            return True

        if attempt < max_retries:
            info(
                f"  Attempt {attempt}/{max_retries}: {display} "
                f"not found (HTTP {http_code}), retrying in {retry_delay}s..."
            )
            time.sleep(retry_delay)

    error(f"  Verification failed: {display} not found after {max_retries} attempts")
    return False


# Common directories to exclude from quality checks
_COMMON_EXCLUDES = [
    ".venv",
    "venv",
    "env",
    ".env",
    "virtualenv",
    ".virtualenv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    "*.egg-info",
    ".eggs",
    "dist",
    "build",
    "wheelhouse",
    ".tox",
    ".nox",
    ".git",
    ".github",
    "node_modules",
    ".npm",
    ".yarn",
    ".pnpm-store",
    ".next",
    ".nuxt",
    ".output",
    ".svelte-kit",
    "target",
    "vendor",
    ".idea",
    ".vscode",
    ".vs",
    "htmlcov",
    "coverage",
    ".coverage",
    ".nyc_output",
    "_build",
    "site",
    ".cache",
    ".tmp",
    "tmp",
    ".temp",
    "temp",
]


def get_exclude_dirs(config_raw: dict[str, Any] | None = None) -> list[str]:
    """Get directories to exclude from quality checks.

    Combines:
      1. Git submodule paths (from .gitmodules)
      2. ci/ and ai/ (always)
      3. Common directories (.venv, node_modules, target, etc.)
      4. Custom paths from quality.exclude_paths config
    """
    excludes: list[str] = []

    gitmodules = Path(".gitmodules")
    if gitmodules.exists():
        for line in gitmodules.read_text().splitlines():
            if "path" in line and "=" in line:
                path = line.split("=", 1)[1].strip()
                if path and Path(path).is_dir():
                    excludes.append(path)

    for submod in ("ci", "ai"):
        if Path(submod).is_dir() and submod not in excludes:
            excludes.append(submod)

    for dirname in _COMMON_EXCLUDES:
        if Path(dirname).exists() and dirname not in excludes:
            excludes.append(dirname)

    if config_raw:
        custom = config_raw.get("quality", {}).get("exclude_paths", [])
        if isinstance(custom, list):
            for path in custom:
                if path and Path(path).is_dir() and path not in excludes:
                    excludes.append(path)

    return excludes
