# Project:   HyperI CI
# File:      src/hyperi_ci/build_targets.py
# Purpose:   The Rust targets a project declares, read in the plan job
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The Rust targets a project lists under ``build.rust.targets``.

The Rust Plan builds its matrix from these: a project that lists targets gets
legs for those only, so one whose release build does not fit the arm64 runner
still releases amd64 (issue #127). An empty list means every target.

The predict-version composite loads this by path on a runner where hyperi-ci is
not installed, so it is stdlib-only and imports nothing heavier than
:mod:`hyperi_ci.project_config`, which is stdlib-only for the same reason.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs the composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
# predict-version loads this file by path on the runner's python3, which may predate 3.14.
from __future__ import annotations

from pathlib import Path
from typing import Any

from hyperi_ci.project_config import read_project_config

#: Where the list lives in the project config.
TARGETS_KEY = "build.rust.targets"


def declared_targets(config: dict[str, Any]) -> list[str]:
    """Return ``build.rust.targets`` from a parsed config.

    Args:
        config: The parsed project config.

    Returns:
        The listed targets, empty when none are listed.

    Raises:
        ValueError: If the key holds something other than a list of strings.

    """
    build = config.get("build")
    rust = build.get("rust") if isinstance(build, dict) else None
    targets = rust.get("targets") if isinstance(rust, dict) else None
    if targets is None:
        return []
    if not isinstance(targets, list) or not all(
        isinstance(target, str) for target in targets
    ):
        raise ValueError(
            f"{TARGETS_KEY} must be a list of target triples, not {targets!r}"
        )
    return [target.strip() for target in targets if target.strip()]


def read_rust_targets(root: Path) -> tuple[list[str], str]:
    """Read the targets the project lists.

    Args:
        root: The checkout root.

    Returns:
        The targets (empty meaning every target), and why the list could not
        be read, else empty. An unreadable list returns no targets.

    """
    project = read_project_config(root)
    if project.data is None:
        return [], project.unreadable
    try:
        return declared_targets(project.data), ""
    except ValueError as exc:
        return [], f"{project.name}: {exc}"
