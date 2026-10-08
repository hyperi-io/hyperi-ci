# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/targets.py
# Purpose:   Cargo workspace metadata and the library-only test
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Cargo workspace metadata and the library-only test, shared so stages agree."""

import json
import tomllib
from pathlib import Path

from hyperi_ci.common import run_cmd


def cargo_metadata(project_dir: Path | None = None) -> dict | None:
    """Parse ``cargo metadata --no-deps``, or None when it cannot be had.

    None also covers cargo being absent, as in the container job, where the
    missing binary raises rather than returning a code (issue #207).

    Args:
        project_dir: Directory to run cargo in. None uses the current one.

    Returns:
        The decoded metadata, or None.

    """
    try:
        result = run_cmd(
            ["cargo", "metadata", "--no-deps", "--format-version=1"],
            check=False,
            capture=True,
            cwd=project_dir,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def rust_is_library(project_dir: Path) -> bool:
    """Return True when no workspace member declares a bin target.

    A feature-gated bin still counts. Without cargo, only the root crate's
    ``src/main.rs``, ``src/bin/*.rs`` and ``[[bin]]`` are checked.

    Args:
        project_dir: Project root holding ``Cargo.toml``.

    Returns:
        True for a library-only crate or workspace, False when there is a
        bin target or no ``Cargo.toml`` at all.

    """
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.exists():
        return False

    metadata = cargo_metadata(project_dir)
    if metadata is not None:
        for package in metadata.get("packages", []):
            for target in package.get("targets", []):
                if "bin" in target.get("kind", []):
                    return False
        return True

    if (project_dir / "src" / "main.rs").exists():
        return False
    bin_dir = project_dir / "src" / "bin"
    if bin_dir.is_dir() and any(p.suffix == ".rs" for p in bin_dir.iterdir()):
        return False
    try:
        manifest = tomllib.loads(cargo_toml.read_text(encoding="utf-8"))
    except Exception:
        return False
    if manifest.get("bin"):
        return False
    return True
