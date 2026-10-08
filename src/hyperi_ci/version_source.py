# Project:   HyperI CI
# File:      src/hyperi_ci/version_source.py
# Purpose:   Where the first version comes from, when there is no tag yet
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Derive a repo's starting version from what the project already declares.

The git tag is the only truth about a released version, so the one question it
cannot answer is what the FIRST tag should be. This module answers it from the
project's own manifest: ``pyproject.toml`` ``[project] version``, ``Cargo.toml``
``[package] version``, or ``package.json`` ``version``. With nothing to read
(Go, or a dynamic-version Python project) it starts at ``DEFAULT_SEED_VERSION``.

The ``VERSION`` file is NOT read for the seed: this tool writes it at build
time, and treating it as input let a stale value pass for the current version
across 14 repos (issue #85).

Stdlib only, with no imports from the rest of the package, because the
``predict-version`` composite action loads this file BY PATH and hatchling
imports it as the build back-end's version source (:func:`build_version`),
neither after a ``pip install``.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# predict-version loads this file BY PATH before any install, on whatever
# python3 the runner has, which may predate our floor.
# predict-version loads this file by path on the runner's python3, which may predate 3.14.
from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

# Greenfield start: 0.x makes no stability promise, and 1.0.0 is a decision.
DEFAULT_SEED_VERSION = "0.1.0"

# Plain X.Y.Z only, so a pre-release or PEP 440 local version (`0.1.0a1`,
# `1.2.3.post1`) falls through to the next manifest.
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# `v` + the released version, including prereleases (`v1.2.0-beta.1`). The `v`
# is required because publish names the release `v{version}`.
_RELEASE_TAG_RE = re.compile(
    r"^v(\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$"
)


def _usable(value: object) -> str | None:
    """Normalise a manifest value to a bare ``X.Y.Z``, or reject it."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().removeprefix("v")
    return candidate if _SEMVER_RE.match(candidate) else None


def load_toml(path: Path) -> dict:
    """Parse a TOML file, or return ``{}`` when it is missing or invalid."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _pyproject_version(path: Path) -> str | None:
    """PEP 621 ``[project] version``, else Poetry's ``[tool.poetry] version``.

    A ``dynamic = ["version"]`` project has no static version, so it is skipped.
    """
    data = load_toml(path)
    project = data.get("project")
    if isinstance(project, dict):
        dynamic = project.get("dynamic")
        declared_dynamic = isinstance(dynamic, list) and "version" in dynamic
        if not declared_dynamic:
            found = _usable(project.get("version"))
            if found:
                return found
    poetry = data.get("tool", {}).get("poetry")
    if isinstance(poetry, dict):
        return _usable(poetry.get("version"))
    return None


def _cargo_version(path: Path) -> str | None:
    """``[package] version``, following ``version.workspace = true`` up.

    A virtual manifest (workspace root with no ``[package]``) keeps the version
    in ``[workspace.package]`` for every member to inherit.
    """
    data = load_toml(path)
    workspace_version = _usable(
        data.get("workspace", {}).get("package", {}).get("version")
    )
    package = data.get("package")
    if isinstance(package, dict):
        found = _usable(package.get("version"))
        if found:
            return found
        # `version.workspace = true` parses as a table, not a string.
        if isinstance(package.get("version"), dict):
            return workspace_version
    return workspace_version


def _package_json_version(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _usable(data.get("version")) if isinstance(data, dict) else None


# The first manifest yielding a usable version wins. Python leads because a
# polyglot repo (a Rust binary with a Python wrapper) is versioned by its pyproject.
_MANIFEST_READERS: tuple[tuple[str, Callable[[Path], str | None]], ...] = (
    ("pyproject.toml", _pyproject_version),
    ("Cargo.toml", _cargo_version),
    ("package.json", _package_json_version),
)


def declared_version(root: Path | None = None) -> tuple[str, str] | None:
    """Read the version the project declares for itself, and which file said so.

    Args:
        root: Project root. Defaults to cwd.

    Returns:
        ``(version, manifest_filename)``, or None when no manifest declares a
        usable plain-semver version.

    """
    base = root or Path.cwd()
    for filename, reader in _MANIFEST_READERS:
        path = base / filename
        if not path.is_file():
            continue
        found = reader(path)
        if found:
            return found, filename
    return None


def seed_version(root: Path | None = None) -> tuple[str, str]:
    """Resolve the version a tag-less repo starts from, and where it came from.

    Args:
        root: Project root. Defaults to cwd.

    Returns:
        ``(version, source)`` where source is a manifest filename or
        ``"default"`` (:data:`DEFAULT_SEED_VERSION`).

    """
    found = declared_version(root)
    return found if found else (DEFAULT_SEED_VERSION, "default")


def latest_tag_version(root: Path | None = None) -> str | None:
    """Read the highest final-release ``vX.Y.Z`` git tag as a bare version, or None.

    The released version, so one behind mid-release. Anything resolving the
    version being released reads ``HYPERCI_VERSION`` instead.
    """
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"],
            cwd=str(root) if root else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # Plain vX.Y.Z only -- a prerelease sorts above its own release under -v:refname.
    for line in result.stdout.splitlines():
        candidate = _usable(line)
        if candidate:
            return candidate
    return None


def tag_version(tag: str) -> str | None:
    """Return the version a release tag names, or None when it names none.

    A retroactive ``tag`` dispatch re-publishes this version. The tagged tree
    cannot answer, because ``VERSION`` and the manifest are committed back after
    the tag (issue #352).
    """
    match = _RELEASE_TAG_RE.match(tag.strip())
    return match.group(1) if match else None


def build_version(root: Path | None = None, *, allow_env: bool = True) -> str:
    """Resolve the version for the build back-end.

    hatchling's ``code`` version source calls this. ``VERSION`` is rendered
    during the run rather than committed, so the order is:

    1. ``HYPERCI_VERSION`` -- the plan job's predicted version.
    2. ``VERSION`` -- written by the stamp step, and carried in the sdist so a
       wheel built from one gets the released number.
    3. The latest release tag -- a checkout with no stamp.
    4. The seed version -- a tag-less repo.

    Args:
        root: Project root. Defaults to cwd, which is where the back-end runs.
        allow_env: Consult ``HYPERCI_VERSION``. Off when reporting on a SPECIFIC
            checkout, because the variable is process-wide, not per-tree.

    Returns:
        A bare ``X.Y.Z``.

    """
    base = root or Path.cwd()

    explicit = _usable(os.environ.get("HYPERCI_VERSION", "")) if allow_env else ""
    if explicit:
        return explicit

    version_file = base / "VERSION"
    if version_file.is_file():
        stamped = _usable(version_file.read_text(encoding="utf-8").strip())
        if stamped:
            return stamped

    tagged = latest_tag_version(base)
    if tagged:
        return tagged

    return seed_version(base)[0]
