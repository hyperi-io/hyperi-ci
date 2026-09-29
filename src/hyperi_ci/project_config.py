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
stdlib-only and imports nothing from the package.

The composite runs its config readers under ``uv run --with pyyaml``, because a
runner's own python3 may have no PyYAML: the ARC vanilla image has neither it
nor ``yq``. When uv cannot supply PyYAML the composite runs them on the
runner's python3 instead, where ``yq`` is the fallback parser. A file
that exists and cannot be parsed is reported with the reason, never read as
empty, because an empty config quietly means "every default".
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs the composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

#: Every spelling of the project config, in the order the first one found wins.
CONFIG_FILES = (
    ".hyperi-ci.yaml",
    ".hyperi-ci.yml",
    ".hypersec-ci.yaml",
    ".hypersec-ci.yml",
)

#: The reason given when this interpreter has no way to parse YAML at all.
NO_PARSER = "neither PyYAML nor yq is available to parse it"


class ProjectConfig(NamedTuple):
    """The project config as read, or why it could not be.

    Attributes:
        data: The parsed mapping, empty when there is no file, None when the
            file exists and could not be read.
        name: The file name, for messages. The canonical spelling when absent.
        problem: Why the file could not be read, else empty.

    """

    data: dict[str, Any] | None
    name: str
    problem: str

    @property
    def unreadable(self) -> str:
        """One line naming the file and the reason, or empty when it was read."""
        if self.data is not None:
            return ""
        return f"{self.name} could not be read: {self.problem}"


def find_config(root: Path) -> Path | None:
    """Return the project config file under ``root``, or None if there is none."""
    for name in CONFIG_FILES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _one_line(text: str) -> str:
    # A workflow command ends at the first newline, so the reason must not carry one.
    return " ".join(text.split()) or "no detail given"


def _as_mapping(parsed: object) -> tuple[dict[str, Any] | None, str]:
    if parsed is None:
        return {}, ""
    if isinstance(parsed, dict):
        return parsed, ""
    return None, f"its top level is a {type(parsed).__name__}, not a mapping"


def _parse_with_yq(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Parse the config with yq, for a runner whose python3 lacks PyYAML."""
    if not shutil.which("yq"):
        return None, NO_PARSER
    result = subprocess.run(
        ["yq", "-o=json", ".", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None, f"yq could not parse it: {_one_line(result.stderr)}"
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"yq returned no JSON: {exc}"
    return _as_mapping(parsed)


def read_config(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Read one config file.

    Args:
        path: Path to the config file.

    Returns:
        The parsed mapping (empty when the file is absent, None when it exists
        and could not be read), and the reason it could not be read, else empty.

    """
    if not path.is_file():
        return {}, ""
    try:
        import yaml
    except ImportError:
        return _parse_with_yq(path)
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, f"it is not valid YAML: {_one_line(str(exc))}"
    return _as_mapping(parsed)


def read_project_config(root: Path) -> ProjectConfig:
    """Read whichever config file the project has.

    A project with no config file at all is legitimate and reads as empty.

    Args:
        root: The checkout root.

    Returns:
        The config, its file name, and why it could not be read.

    """
    path = find_config(root)
    if path is None:
        return ProjectConfig({}, CONFIG_FILES[0], "")
    data, problem = read_config(path)
    return ProjectConfig(data, path.name, problem)
