# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/build.py
# Purpose:   TypeScript build handler
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript build handler."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success
from hyperi_ci.config import CIConfig


def _detect_package_manager() -> str:
    if Path("pnpm-lock.yaml").exists():
        return "pnpm"
    if Path("yarn.lock").exists():
        return "yarn"
    return "npm"


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run TypeScript build."""
    info("Building TypeScript project...")
    pm = _detect_package_manager()

    result = subprocess.run([pm, "run", "build"])
    if result.returncode != 0:
        error("TypeScript build failed")
        return result.returncode

    success("TypeScript build complete")
    return 0
