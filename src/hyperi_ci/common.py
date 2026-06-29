# Project:   HyperI CI
# File:      src/hyperi_ci/common.py
# Purpose:   Shared utilities for CI scripts (output, subprocess, exclusions)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared utilities for HyperI CI.

Uses hyperi-pylib logger for structured output with automatic environment
detection (GitHub Actions workflow commands, Solarized terminal, plain CI).
"""

from __future__ import annotations

import os
import re
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


def sanitize_ref_name(ref: str) -> str:
    """Sanitize a git ref name for use in file paths.

    Replaces '/' (from branch names like 'fix/reconcile-release') with '-'
    so the ref can be safely used in artifact filenames.
    """
    return ref.replace("/", "-")


def resolve_release_version() -> str | None:
    """Resolve the version being released - single SSoT for every stage.

    Precedence (issue #27): the Plan job's predicted ``next-version``, threaded
    in via ``HYPERCI_VERSION``, is authoritative — the same value Build stamps
    and Tag-and-Publish tags, so every job in a run agrees. The committed
    ``VERSION`` file is a fallback only (local runs); it is stale in CI now that
    stamping is central and not committed back. Leading ``v`` is stripped.
    Returns None when neither is set (caller decides whether that's fatal).

    Container, binary and registry publish all call this — do NOT re-implement
    version reading per stage, or they drift (which is exactly how the GH
    release shipped a stale tag once set-version.py was removed).
    """
    explicit = os.environ.get("HYPERCI_VERSION", "").strip()
    if explicit:
        return explicit.removeprefix("v")
    version_file = Path("VERSION")
    if version_file.exists():
        value = version_file.read_text().strip()
        if value:
            return value.removeprefix("v")
    return None


_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def explicit_version(value: str | None) -> str | None:
    """Bare ``X.Y.Z`` if ``value`` is an explicit version, else None.

    The from-head ``bump`` channel doubles as an explicit-version override
    (``hyperi-ci publish --version X.Y.Z``): ``auto``/``patch``/``minor`` are
    bump levels resolved at release time; a bare semver is taken verbatim and
    tagged at HEAD. A leading ``v`` is tolerated. Only plain ``X.Y.Z`` is
    accepted (no pre-release / build metadata) — releases here are always
    plain semver and the tag format is ``v${version}``.
    """
    candidate = value.strip().removeprefix("v") if value else ""
    return candidate if _SEMVER_RE.match(candidate) else None


def is_ci() -> bool:
    """Detect if running in a CI/runner environment."""
    return any(
        os.environ.get(v)
        for v in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE")
    )


def is_github_actions() -> bool:
    """Detect if running in GitHub Actions specifically."""
    return bool(os.environ.get("GITHUB_ACTIONS"))


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
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=run_env,
    )


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
