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
import tomllib
from contextlib import contextmanager
from pathlib import Path

import tomli_w

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig

# Directories/files that are never part of a Python package — AI coding agent dirs,
# org submodules, and tool dirs. Injected into hatchling sdist exclusions at build
# time so every project gets these for free without repeating them in pyproject.toml.
#
# AI agent paths: Claude Code, Cursor, Gemini, Copilot, Windsurf, etc.
# Org submodules: hyperi-ai (standards), ci (old CI replaced by hyperi-ci).
_STANDARD_SDIST_EXCLUDES = [
    # Claude Code
    "/.claude",
    "/CLAUDE.md",
    # Cursor
    "/.cursor",
    "/CURSOR.md",
    # Gemini
    "/.gemini",
    "/GEMINI.md",
    # GitHub Copilot
    "/.github/copilot-instructions.md",
    # Windsurf
    "/.windsurf",
    # Shared AI context file (symlinked as CLAUDE.md, CURSOR.md, etc.)
    "/STATE.md",
    # Org AI standards submodule
    "/hyperi-ai",
    # Legacy CI submodule (replaced by hyperi-ci)
    "/ci",
]


@contextmanager
def _inject_sdist_excludes(pyproject_path: Path):
    """Temporarily add standard sdist exclusions to pyproject.toml."""
    original = pyproject_path.read_bytes()
    try:
        data = tomllib.loads(original.decode())
        sdist = (
            data.setdefault("tool", {})
            .setdefault("hatch", {})
            .setdefault("build", {})
            .setdefault("targets", {})
            .setdefault("sdist", {})
        )
        existing = sdist.get("exclude", [])
        sdist["exclude"] = list({*existing, *_STANDARD_SDIST_EXCLUDES})
        pyproject_path.write_bytes(tomli_w.dumps(data).encode())
        yield
    finally:
        pyproject_path.write_bytes(original)


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
    pyproject = Path("pyproject.toml")
    if pyproject.exists():
        ctx = _inject_sdist_excludes(pyproject)
    else:
        from contextlib import nullcontext

        ctx = nullcontext()

    with ctx:
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
