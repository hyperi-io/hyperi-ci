# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/build.py
# Purpose:   Python build handler (wheel, sdist, nuitka)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python build handler.

Builds Python packages using uv/pip wheel or Nuitka for compiled binaries.
"""

import re
import subprocess
import tomllib
from contextlib import contextmanager
from pathlib import Path

import tomli_w

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig

# Directories/files that are never part of a Python package -- AI coding agent dirs,
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

# hatchling's DEFAULT_PATTERN (hatchling/version/core.py), so the stamp lands
# where the build reads the version from.
_HATCH_DEFAULT_VERSION_PATTERN = (
    r"""(?i)^(__version__|VERSION) *= *(['"])v?(?P<version>.+?)\2"""
)


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


def stamp_manifest(version: str, root: Path) -> None:
    """Stamp `version` into pyproject.toml, or the file hatch reads it from.

    Static-version projects (PEP 621 `[project] version = "..."`) get the
    rewrite. A dynamic version read by hatch from a file
    (``[tool.hatch.version] path``) gets that file stamped instead, see
    `_stamp_hatch_version`. Other dynamic-version backends are left alone.

    Raises:
        StampError: The hatch version file cannot be stamped, so the wheel
            would carry the wrong version.

    """
    from hyperi_ci.stamp import replace_toml_table_version

    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return
    text = pyproject.read_text(encoding="utf-8")
    new_text = replace_toml_table_version(text, "project", version)
    if new_text != text:
        pyproject.write_text(new_text, encoding="utf-8", newline="\n")
        info(f"Stamped pyproject.toml: {version}")

    data = tomllib.loads(text)
    if "version" not in data.get("project", {}).get("dynamic", []):
        return
    hatch_version = data.get("tool", {}).get("hatch", {}).get("version")
    if isinstance(hatch_version, dict):
        _stamp_hatch_version(version, root, hatch_version)


def _stamp_hatch_version(version: str, root: Path, settings: dict) -> None:
    """Stamp the file a ``[tool.hatch.version]`` regex source reads.

    Matches the way hatchling reads it: ``pattern`` (or hatchling's default)
    searched in multiline mode, and the ``version`` group replaced, which is
    what ``hatch version <v>`` writes. A ``vcs`` source takes the version from
    git and a ``code`` source evaluates a file at build time, so neither has a
    literal to stamp. Any other source is refused.

    Raises:
        StampError: The source is unsupported, the file is missing, or the
            pattern finds no version in it.

    """
    from hyperi_ci.stamp import StampError

    source = settings.get("source", "regex")
    if source == "vcs":
        warn(
            "pyproject.toml: [tool.hatch.version] source = 'vcs' - hatch-vcs "
            "derives the version from git, so hyperi-ci stamped nothing for it"
        )
        return
    if source == "code":
        info(
            "pyproject.toml: [tool.hatch.version] source = 'code' - the version "
            "is evaluated at build time, so there is no literal to stamp"
        )
        return
    if source != "regex":
        raise StampError(
            f"pyproject.toml: [tool.hatch.version] source = {source!r} cannot "
            f"be stamped, so the wheel would not carry {version}"
        )

    rel_path = settings.get("path")
    if not isinstance(rel_path, str) or not rel_path:
        raise StampError("pyproject.toml: [tool.hatch.version] names no path")
    target = root / rel_path
    if not target.is_file():
        raise StampError(f"[tool.hatch.version] path {rel_path} does not exist")

    pattern = settings.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        pattern = _HATCH_DEFAULT_VERSION_PATTERN
    text = target.read_text(encoding="utf-8")
    try:
        match = re.search(pattern, text, flags=re.MULTILINE)
    except re.error as exc:
        raise StampError(
            f"[tool.hatch.version] pattern is not a valid regex: {exc}"
        ) from exc
    if match is None or "version" not in match.groupdict():
        raise StampError(
            f"{rel_path}: no version matches the [tool.hatch.version] pattern, "
            f"so the wheel would not carry {version}"
        )

    start, end = match.span("version")
    new_text = text[:start] + version + text[end:]
    if new_text != text:
        target.write_text(new_text, encoding="utf-8", newline="\n")
    info(f"Stamped {rel_path}: {version}")
