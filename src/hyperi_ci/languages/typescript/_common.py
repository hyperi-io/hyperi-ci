# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/_common.py
# Purpose:   Shared TypeScript/Node utilities
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared utilities for TypeScript language handlers."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import info, warn

# turbo holds each task's output until the task ends when it detects CI, so a
# cancelled or hung task leaves nothing in the log (issue #265).
_STREAM_OUTPUT_ENV = {"TURBO_LOG_ORDER": "stream"}


def package_script_env() -> dict[str, str]:
    """Return the variables to add when running a package script.

    A value the project already exported wins, so a repo that wants turbo's
    grouped output keeps it.

    Returns:
        Environment overlay for the package-script subprocess.

    """
    return {k: v for k, v in _STREAM_OUTPUT_ENV.items() if k not in os.environ}


def _corepack_enable() -> bool:
    """Enable Corepack, retrying into ``~/.corepack/bin`` if Node's bin is read-only.

    The retry directory is added to PATH.

    Returns:
        True if corepack was enabled successfully.

    """
    if not shutil.which("corepack"):
        warn("corepack not found on PATH")
        return False

    cp = subprocess.run(
        ["corepack", "enable"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if cp.returncode == 0:
        info("  corepack enabled")
        return True

    stderr = cp.stderr.strip() if cp.stderr else "unknown error"
    warn(f"corepack enable failed ({stderr}) -- retrying with user directory")

    user_dir = Path.home() / ".corepack" / "bin"
    user_dir.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(
        ["corepack", "enable", "--install-directory", str(user_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if cp.returncode == 0:
        os.environ["PATH"] = str(user_dir) + os.pathsep + os.environ.get("PATH", "")
        info(f"  corepack enabled (install-directory={user_dir})")
        return True

    warn("corepack enable failed with user directory too")
    return False


def read_package_json(project_dir: Path | None = None) -> dict[str, object]:
    """Return package.json as a dict, empty when it is missing or not a JSON object.

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        The parsed manifest, or an empty dict.

    """
    pkg = (project_dir or Path.cwd()) / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def package_scripts(project_dir: Path | None = None) -> dict[str, object]:
    """Return package.json's ``scripts`` table, empty when there is none."""
    scripts = read_package_json(project_dir).get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def pinned_package_manager(project_dir: Path | None = None) -> str | None:
    """Return the package manager pinned by package.json, or None.

    The ``packageManager`` field is Corepack's contract: when present, only a
    Corepack shim honours the pinned version.

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        One of pnpm/yarn/npm, or None when nothing valid is pinned.

    """
    pm_raw = read_package_json(project_dir).get("packageManager")
    if isinstance(pm_raw, str) and pm_raw:
        name = pm_raw.split("@")[0].strip().lower()
        if name in ("pnpm", "yarn", "npm"):
            return name
    return None


def ensure_pm_available(pm: str, project_dir: Path | None = None) -> bool:
    """Ensure a package manager usable by THIS project is on PATH.

    A ``packageManager`` pin makes a bare global binary of the same name refuse
    to run the project ("the current global version of Yarn is 1.22.22"), so a
    pinned project resolves its PM through Corepack. Any PATH binary serves an
    unpinned one.

    Args:
        pm: Package manager name (npm, yarn, pnpm).
        project_dir: Project root, used to read the packageManager pin.

    Returns:
        True if a usable PM is available, False if all attempts failed.

    """
    pinned = pinned_package_manager(project_dir) == pm
    if pm == "npm" and not pinned:
        return True
    if not pinned and shutil.which(pm):
        return True

    # A corepack bin dir from an earlier step already holds pin-safe shims.
    user_dir = Path.home() / ".corepack" / "bin"
    if user_dir.is_dir():
        os.environ["PATH"] = str(user_dir) + os.pathsep + os.environ.get("PATH", "")
        found = shutil.which(pm)
        if found and (not pinned or found.startswith(str(user_dir))):
            info(f"  {pm} found in {user_dir}")
            return True

    if _corepack_enable():
        return True

    # Without Corepack a global binary is still tried, so a pin mismatch fails
    # the install loudly.
    return shutil.which(pm) is not None


def detect_package_manager(project_dir: Path | None = None) -> str:
    """Detect the package manager from the ``packageManager`` pin, lock file, else npm.

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        One of: pnpm, yarn, npm

    """
    root = project_dir or Path.cwd()

    pinned = pinned_package_manager(root)
    if pinned:
        return pinned

    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "package-lock.json").exists():
        return "npm"

    return "npm"


def detect_yarn_version(project_dir: Path | None = None) -> int:
    """Detect whether the project uses Yarn Classic (1) or Yarn Berry (2+).

    Reads the packageManager pin, else runs ``yarn --version``.

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        Major version number (1 or 2+).

    """
    root = project_dir or Path.cwd()

    pm_raw = read_package_json(root).get("packageManager")
    if isinstance(pm_raw, str) and pm_raw.startswith("yarn@"):
        try:
            return int(pm_raw.split("@")[1].split(".")[0])
        except ValueError:
            pass

    try:
        result = subprocess.run(
            ["yarn", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=root,
        )
        if result.returncode == 0:
            major = int(result.stdout.strip().split(".")[0])
            return major
    except (FileNotFoundError, ValueError, IndexError):
        pass

    return 1


def yarn_frozen_flag(project_dir: Path | None = None) -> str:
    """Return ``--immutable`` for Yarn 2+, else ``--frozen-lockfile``.

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        The CLI flag.

    """
    version = detect_yarn_version(project_dir)
    if version >= 2:
        return "--immutable"
    return "--frozen-lockfile"
