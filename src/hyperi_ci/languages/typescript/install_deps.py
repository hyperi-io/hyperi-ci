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

import subprocess
from pathlib import Path

from hyperi_ci.common import error, info

from ._common import detect_package_manager, ensure_pm_available, yarn_frozen_flag


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

    if not ensure_pm_available(pm):
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
