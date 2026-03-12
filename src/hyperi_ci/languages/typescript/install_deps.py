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


def _enable_corepack(pm: str) -> None:
    """Enable Corepack so that packageManager-pinned versions are respected.

    Skips if the target package manager is already on PATH (e.g. pre-installed
    on ARC runner images). Warns instead of failing if corepack enable fails —
    the PM may still work without it (permissions issue on system Node installs).
    """
    if pm != "npm" and shutil.which(pm):
        info(f"  {pm} already on PATH — skipping corepack enable")
        return

    if not shutil.which("corepack"):
        warn("corepack not found on PATH — skipping")
        return

    cp = subprocess.run(["corepack", "enable"], capture_output=True, text=True)
    if cp.returncode != 0:
        stderr = cp.stderr.strip() if cp.stderr else "unknown error"
        warn(f"corepack enable failed ({stderr}) — continuing with system PM")
        return

    info("  corepack enabled")


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

    _enable_corepack(pm)

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
