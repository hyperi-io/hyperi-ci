# Project:   HyperI CI
# File:      src/hyperi_ci/version_source.py
# Purpose:   Where the first version comes from, when there is no tag yet
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Derive a repo's starting version from what the project already declares.

The git tag is the only truth about a released version (tag-on-publish: a
tag exists iff the artefact is in the registry). That leaves exactly one
question a tag cannot answer — what should the FIRST tag be? — and this
module is the single place that answers it.

The answer comes from the project's own manifest, because a project adopting
hyperi-ci usually already has a version it calls itself: ``pyproject.toml``
``[project] version``, ``Cargo.toml`` ``[package] version``, ``package.json``
``version``. Nothing to read (Go has no manifest version; a dynamic-version
Python project has no static one) means a greenfield start at
``DEFAULT_SEED_VERSION``.

Deliberately NOT read here: the ``VERSION`` file. It is a build-time artefact
this tool writes, not an input — treating it as one is what let a value frozen
in May 2026 masquerade as the current version across 14 repos (issue #85).

Stdlib only, and no imports from the rest of the package. Two loaders depend
on that: the ``predict-version`` composite action loads this file BY PATH out
of the action checkout, and hatchling imports it as the build back-end's
version source (:func:`build_version`) — neither has run a ``pip install``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

# Greenfield start. 0.1.0 rather than 1.0.0: a project with nothing to
# declare has made no stability promise, and semver reserves 0.x for
# exactly that. Reaching 1.0.0 stays a decision someone makes.
DEFAULT_SEED_VERSION = "0.1.0"

# Plain X.Y.Z only. Release tags here are always plain semver (`v${version}`),
# so a pre-release or PEP 440 local version (`0.1.0a1`, `1.2.3.post1`) is not a
# usable seed and falls through to the next manifest.
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _usable(value: object) -> str | None:
    """Normalise a manifest value to a bare ``X.Y.Z``, or reject it."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().removeprefix("v")
    return candidate if _SEMVER_RE.match(candidate) else None


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _pyproject_version(path: Path) -> str | None:
    """PEP 621 ``[project] version``, else Poetry's ``[tool.poetry] version``.

    A ``dynamic = ["version"]`` project has no static version to read — the
    build back-end computes it — so it is skipped rather than guessed at.
    """
    data = _load_toml(path)
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

    A virtual manifest (workspace root with no ``[package]``) is the
    dfe-archiver shape: the version lives in ``[workspace.package]`` and every
    member inherits it.
    """
    data = _load_toml(path)
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


# Ordered: the first manifest present that yields a usable version wins.
# Python leads because a polyglot repo (a Rust binary with a Python wrapper)
# is versioned by whatever its pyproject says.
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
        ``"default"``. Always returns a version — a repo with nothing to
        declare starts at :data:`DEFAULT_SEED_VERSION`.

    """
    found = declared_version(root)
    return found if found else (DEFAULT_SEED_VERSION, "default")


def latest_tag_version(root: Path | None = None) -> str | None:
    """Read the highest ``v*`` git tag as a bare version, or None.

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
    return _usable(result.stdout.splitlines()[0].strip())


def build_version(root: Path | None = None) -> str:
    """Resolve the version for the build back-end.

    hatchling's ``code`` version source calls this. ``VERSION`` is rendered
    during the run rather than committed, so the back-end cannot assume a file
    is there, and the fallbacks have to cover every place a build starts:

    1. ``HYPERCI_VERSION`` — the plan job's predicted version, the same value
       every other stage in the run agrees on.
    2. ``VERSION`` — written moments earlier by the stamp step, and carried in
       the sdist so a wheel built from one gets the released number.
    3. The latest ``v*`` tag — a developer building a checkout with no stamp.
    4. The seed version — a tag-less repo, which has nothing else to offer.

    Args:
        root: Project root. Defaults to cwd, which is where the back-end runs.

    Returns:
        A bare ``X.Y.Z``.

    """
    base = root or Path.cwd()

    explicit = _usable(os.environ.get("HYPERCI_VERSION", ""))
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
