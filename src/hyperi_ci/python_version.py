# Project:   HyperI CI
# File:      src/hyperi_ci/python_version.py
# Purpose:   Resolve the Python version a project is built and tested on
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve the Python version a project is built and tested on.

The fleet default in ``config/versions.yaml`` answers one question: what does a
project that declares nothing get? It is not an instruction to build every
project on that version. A repo declaring ``requires-python = ">=3.12"`` that
gets tested on 3.14 is tested on an interpreter it does not support, and ships
a wheel whose own advertised floor never ran (issue #150).

Resolution, the project's own declaration first:

1. A pegged ``.python-version`` file -- someone wrote a version down, so it is
   not a guess.
2. The ``requires-python`` FLOOR from ``pyproject.toml``. The floor, not the
   newest version satisfying it: testing above the floor hides the bug this
   exists to catch, a 3.14-only feature reaching a repo that promises 3.12.
3. An explicitly requested version.
4. The fleet default.

uv honours (1) natively and needs nothing from us. It resolves (2) to the
NEWEST satisfying interpreter, which is the defect, so the floor is computed
here and handed to uv explicitly.

A version resolves to ``major.minor``. The patch is deliberately dropped: it is
the runner's to choose, and pinning it turns every upstream patch release into
a manifest edit.

Stdlib only, and no imports from the rest of the package: the
``predict-version`` composite loads this file BY PATH in a job where hyperi-ci
is not installed, the same constraint :mod:`hyperi_ci.version_source` carries.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# A lower bound, and only a lower bound. `>=3.12`, `~=3.12` and `==3.12.*` all
# state one; `!=` and a bare `<` do not, and `>3.11` names a version that is
# excluded rather than the first one allowed, so none of them are read here.
_LOWER_BOUND_RE = re.compile(r"(?:>=|~=|==)\s*(\d+)\.(\d+)")

# The first version-shaped token on the line. `.python-version` also accepts an
# implementation prefix (`pypy@3.10`, `cpython-3.12`), and the patch segment is
# optional.
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def _as_major_minor(major: str, minor: str) -> str:
    return f"{major}.{minor}"


def pegged_version(root: Path) -> str | None:
    """Return the version a ``.python-version`` file pegs, or None.

    Read verbatim as an instruction: a file naming a version the project does
    not otherwise support is still what the project asked for.
    """
    path = root / ".python-version"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _VERSION_RE.search(line)
        if match:
            return _as_major_minor(*match.groups())
    return None


def floor_from_specifier(spec: str) -> str | None:
    """Return the lowest version a ``requires-python`` specifier allows, or None.

    Several lower bounds can appear across a specifier set, so the lowest wins:
    that is the oldest interpreter the specifier promises to run on.

    Args:
        spec: A PEP 440 specifier set, such as ``">=3.12,<4.0"``. PyPI publishes
            one per release file, which is how a caller reads the floor of a
            release it is not running.

    Returns:
        ``major.minor``, or None when the specifier names no lower bound.

    """
    bounds = [(int(a), int(b)) for a, b in _LOWER_BOUND_RE.findall(spec)]
    if not bounds:
        return None
    major, minor = min(bounds)
    return _as_major_minor(str(major), str(minor))


def requires_python_floor(root: Path) -> str | None:
    """Return the lowest version ``requires-python`` allows, or None.

    Several lower bounds can appear across a specifier set, so the lowest wins:
    that is the oldest interpreter the project promises to run on, and the one
    a CI run has to prove.
    """
    path = root / "pyproject.toml"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    spec = project.get("requires-python")
    if not isinstance(spec, str):
        return None
    return floor_from_specifier(spec)


def resolve(
    root: Path | None = None,
    *,
    requested: str = "",
    default: str = "",
) -> tuple[str, str]:
    """Resolve the Python version for a project, and say where it came from.

    Args:
        root: Project root. Defaults to cwd.
        requested: An explicitly asked-for version. Ranks BELOW the project's
            own declaration -- a repo that states its floor has answered this
            question, and a caller overriding it is how the floor stops being
            tested.
        default: The fleet default, used when the project declares nothing.

    Returns:
        ``(version, source)``. ``version`` is empty only when nothing declared
        one and no default was given, which the caller must treat as "leave the
        interpreter choice alone" rather than as a version.

    """
    base = root or Path.cwd()

    pegged = pegged_version(base)
    if pegged:
        return pegged, ".python-version"

    floor = requires_python_floor(base)
    if floor:
        return floor, "requires-python"

    match = _VERSION_RE.search(requested)
    if match:
        return _as_major_minor(*match.groups()), "requested"

    match = _VERSION_RE.search(default)
    if match:
        return _as_major_minor(*match.groups()), "default"

    return "", "unresolved"
