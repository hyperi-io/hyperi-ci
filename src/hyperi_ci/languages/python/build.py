# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/build.py
# Purpose:   Python build handler (wheel, sdist, nuitka)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python build handler.

Builds Python packages using uv/pip wheel or Nuitka for compiled binaries.
"""

from __future__ import annotations

import subprocess

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Python build.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).
    """
    strategy = (extra_env or {}).get("BUILD_STRATEGY", "native")
    info(f"Building Python package (strategy: {strategy})...")

    if strategy == "nuitka":
        return _build_nuitka(config)
    return _build_native(config)


def _build_native(config: CIConfig) -> int:
    """Build wheel and sdist using uv."""
    result = subprocess.run(
        ["uv", "build"],
        capture_output=False,
    )
    if result.returncode != 0:
        error("Python build failed")
        return result.returncode

    success("Python build complete")
    return 0


def _build_nuitka(config: CIConfig) -> int:
    """Build compiled binary using Nuitka."""
    info("Nuitka build not yet implemented in hyperi-ci")
    warn("Nuitka builds will be ported from the old CI system")
    return 1
