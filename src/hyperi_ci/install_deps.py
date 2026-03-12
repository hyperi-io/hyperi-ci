# Project:   HyperI CI
# File:      src/hyperi_ci/install_deps.py
# Purpose:   Language-specific dependency install (e.g. npm/yarn/pnpm for TypeScript)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install project dependencies for a language (distinct from native system deps)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hyperi_ci.common import error, info
from hyperi_ci.languages.typescript._common import detect_package_manager


def install_deps_typescript(project_dir: Path | None = None) -> int:
    """Install TypeScript/Node dependencies using the project's package manager.

    Detects package manager from package.json packageManager field or lock files,
    enables Corepack if needed, and runs the appropriate install command.
    """
    root = project_dir or Path.cwd()

    # Enable Corepack so yarn/pnpm respect packageManager field
    cp = subprocess.run(["corepack", "enable"], capture_output=True, text=True)
    if cp.returncode != 0:
        error("corepack enable failed")
        if cp.stderr:
            print(cp.stderr)
        return cp.returncode

    pm = detect_package_manager(root)
    info(f"Using {pm} (detected from package.json or lock file)")

    if pm == "npm":
        cmd = ["npm", "ci"]
        if not (root / "package-lock.json").exists():
            cmd = ["npm", "install"]
    elif pm == "pnpm":
        cmd = ["pnpm", "install", "--frozen-lockfile"]
    else:
        cmd = ["yarn", "install", "--frozen-lockfile"]

    result = subprocess.run(cmd, cwd=root)
    if result.returncode != 0:
        error(f"{pm} install failed")
        return result.returncode

    return 0


def install_deps(language: str, project_dir: Path | None = None) -> int:
    """Install deps for the given language. Returns 0 on success."""
    if language == "typescript":
        return install_deps_typescript(project_dir)
    error(f"install-deps not implemented for {language}")
    return 1
