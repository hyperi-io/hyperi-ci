# Project:   HyperI CI
# File:      src/hyperi_ci/project_config.py
# Purpose:   Find and read a project's CI config without hyperi-ci installed
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Find and read a project's ``.hyperi-ci.yaml`` with the standard library.

:func:`hyperi_ci.config.load_config` reads the same files in the same order,
from :data:`CONFIG_FILES`. This module exists for the predict-version composite,
which loads it by path on a runner where hyperi-ci is not installed, so it is
stdlib-only and imports nothing from the package. PyYAML is used when the
runner's python3 has it, else ``yq``.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs the composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

#: Every spelling of the project config, in the order the first one found wins.
CONFIG_FILES = (
    ".hyperi-ci.yaml",
    ".hyperi-ci.yml",
    ".hypersec-ci.yaml",
    ".hypersec-ci.yml",
)


def find_config(root: Path) -> Path | None:
    """Return the project config file under ``root``, or None if there is none."""
    for name in CONFIG_FILES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _parse_with_yq(path: Path) -> dict[str, Any] | None:
    """Parse the config with yq, for a runner whose python3 lacks PyYAML."""
    if not shutil.which("yq"):
        return None
    result = subprocess.run(
        ["yq", "-o=json", ".", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def read_config(path: Path) -> dict[str, Any] | None:
    """Read one config file.

    Args:
        path: Path to the config file.

    Returns:
        The parsed mapping, empty when the file is absent, and None when the
        file exists and could not be read.

    """
    if not path.is_file():
        return {}
    try:
        import yaml
    except ImportError:
        return _parse_with_yq(path)
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if parsed is None:
        return {}
    return parsed if isinstance(parsed, dict) else None


def read_project_config(root: Path) -> tuple[dict[str, Any] | None, str]:
    """Read whichever config file the project has.

    Args:
        root: The checkout root.

    Returns:
        The parsed mapping (empty when there is no file, None when the file
        could not be read), and the file name, for messages.

    """
    path = find_config(root)
    if path is None:
        return {}, CONFIG_FILES[0]
    return read_config(path), path.name
