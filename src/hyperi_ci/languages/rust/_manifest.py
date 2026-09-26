# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/_manifest.py
# Purpose:   Facts about the root Cargo.toml that decide cargo's package scope
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Read the root Cargo.toml for what a bare cargo command covers."""

import tomllib
from pathlib import Path


def is_root_package_workspace(project_dir: Path | None = None) -> bool:
    """Return True when a bare cargo command covers only the root package.

    That is a root manifest with both ``[package]`` and ``[workspace]``. A
    virtual workspace covers every member by default, so it answers False. So
    does a workspace that sets ``default-members``: that is the repo's own
    choice of what a bare command runs, and ``--workspace`` would override it.
    A missing or unparseable manifest answers False and leaves cargo to report
    it.

    Args:
        project_dir: Directory holding the root Cargo.toml; the cwd if None.

    Returns:
        Whether cargo needs ``--workspace`` to reach every member.

    """
    manifest = (project_dir or Path.cwd()) / "Cargo.toml"
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    workspace = data.get("workspace")
    return (
        isinstance(data.get("package"), dict)
        and isinstance(workspace, dict)
        and "default-members" not in workspace
    )
