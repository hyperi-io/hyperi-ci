# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/_common.py
# Purpose:   Shared TypeScript/Node utilities
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared utilities for TypeScript language handlers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def detect_package_manager(project_dir: Path | None = None) -> str:
    """Detect which package manager the project uses.

    Priority:
      1. package.json "packageManager" field (authoritative, used by Corepack)
      2. Lock file presence (pnpm-lock.yaml, yarn.lock, package-lock.json)
      3. Default to npm

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        One of: pnpm, yarn, npm
    """
    root = project_dir or Path.cwd()
    pkg = root / "package.json"

    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            pm_raw = data.get("packageManager")
            if isinstance(pm_raw, str) and pm_raw:
                name = pm_raw.split("@")[0].strip().lower()
                if name in ("pnpm", "yarn", "npm"):
                    return name
        except (json.JSONDecodeError, KeyError):
            pass

    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "package-lock.json").exists():
        return "npm"

    return "npm"


def detect_yarn_version(project_dir: Path | None = None) -> int:
    """Detect whether the project uses Yarn Classic (1) or Yarn Berry (2+).

    Checks packageManager field for version, then falls back to running
    ``yarn --version``. Returns 1 for Classic, 2 for Berry/modern.

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        Major version number (1 or 2+).
    """
    root = project_dir or Path.cwd()
    pkg = root / "package.json"

    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            pm_raw = data.get("packageManager", "")
            if isinstance(pm_raw, str) and pm_raw.startswith("yarn@"):
                version_str = pm_raw.split("@")[1].split(".")[0]
                return int(version_str)
        except (json.JSONDecodeError, KeyError, ValueError, IndexError):
            pass

    # Fall back to asking yarn itself
    try:
        result = subprocess.run(
            ["yarn", "--version"],
            capture_output=True,
            text=True,
            cwd=root,
        )
        if result.returncode == 0:
            major = int(result.stdout.strip().split(".")[0])
            return major
    except (FileNotFoundError, ValueError, IndexError):
        pass

    return 1


def yarn_frozen_flag(project_dir: Path | None = None) -> str:
    """Return the correct frozen-install flag for the detected Yarn version.

    Yarn Classic (v1): ``--frozen-lockfile``
    Yarn Berry (v2+): ``--immutable``

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        The appropriate CLI flag string.
    """
    version = detect_yarn_version(project_dir)
    if version >= 2:
        return "--immutable"
    return "--frozen-lockfile"
