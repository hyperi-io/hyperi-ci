# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/build.py
# Purpose:   TypeScript build handler
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript build handler."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.typescript._common import (
    detect_package_manager,
    ensure_pm_available,
)


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run TypeScript build."""
    info("Building TypeScript project...")
    pm = detect_package_manager()
    if not ensure_pm_available(pm):
        error(f"{pm} is not available and could not be installed")
        return 1

    result = subprocess.run([pm, "run", "build"])
    if result.returncode != 0:
        error("TypeScript build failed")
        return result.returncode

    success("TypeScript build complete")
    return 0


def stamp_manifest(version: str, root: Path) -> None:
    """Stamp `version` into package.json's top-level "version".

    Regex-rewrites the first `"version": "..."` (the package's own field —
    dependency specs use `"<name>": "<range>"`, never a bare `"version"`
    key) so the file's formatting is preserved exactly.
    """
    pkg = root / "package.json"
    if not pkg.exists():
        return
    text = pkg.read_text()
    new_text = re.sub(
        r'("version"\s*:\s*)"[^"]*"',
        rf'\g<1>"{version}"',
        text,
        count=1,
    )
    if new_text != text:
        pkg.write_text(new_text)
        info(f"Stamped package.json: {version}")
