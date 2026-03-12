# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/install_deps.py
# Purpose:   Install TypeScript/Node project dependencies
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install TypeScript/Node project dependencies.

Detects the package manager (npm, yarn, pnpm) from package.json or lock files,
enables Corepack if needed, and runs the appropriate install command with
lockfile enforcement.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, warn

from ._common import detect_package_manager, yarn_frozen_flag


def _ensure_pm_available(pm: str) -> bool:
    """Ensure the package manager binary is available on PATH.

    For non-npm package managers (yarn, pnpm), tries in order:
      1. Already on PATH (e.g. pre-installed on ARC runner images) — done
      2. ``corepack enable`` to activate the PM via Node's built-in Corepack
      3. ``npm install -g <pm>`` as a last resort

    Args:
        pm: Package manager name (npm, yarn, pnpm).

    Returns:
        True if the PM is available, False if all attempts failed.
    """
    if pm == "npm" or shutil.which(pm):
        if pm != "npm":
            info(f"  {pm} already on PATH — skipping corepack enable")
        return True

    if shutil.which("corepack"):
        cp = subprocess.run(["corepack", "enable"], capture_output=True, text=True)
        if cp.returncode == 0:
            info("  corepack enabled")
            return True
        stderr = cp.stderr.strip() if cp.stderr else "unknown error"
        warn(f"corepack enable failed ({stderr})")

    info(f"  Installing {pm} globally via npm")
    npm = subprocess.run(["npm", "install", "-g", pm], capture_output=True, text=True)
    if npm.returncode == 0 and shutil.which(pm):
        info(f"  {pm} installed successfully")
        return True

    return False


def run(project_dir: Path | None = None) -> int:
    """Install TypeScript/Node dependencies using the detected package manager.

    Detects the package manager, enables Corepack if needed, and runs
    the appropriate install command.

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        Exit code (0 = success).
    """
    root = project_dir or Path.cwd()

    pm = detect_package_manager(root)
    info(f"Using {pm} (detected from package.json or lock file)")

    if not _ensure_pm_available(pm):
        error(f"{pm} is not available and could not be installed")
        return 1

    if pm == "npm":
        if (root / "package-lock.json").exists():
            cmd = ["npm", "ci"]
        else:
            cmd = ["npm", "install"]
    elif pm == "pnpm":
        cmd = ["pnpm", "install", "--frozen-lockfile"]
    else:
        flag = yarn_frozen_flag(root)
        cmd = ["yarn", "install", flag]

    result = subprocess.run(cmd, cwd=root)
    if result.returncode != 0:
        error(f"{pm} install failed")
        return result.returncode

    return 0
