# Project:   HyperI CI
# File:      src/hyperi_ci/python_version.py
# Purpose:   Resolve the Python version a project is built and tested on
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve the Python version a project is built and tested on.

The fleet default in ``config/versions.yaml`` is only what a project that
declares nothing gets. A repo declaring ``requires-python = ">=3.12"`` that is
tested on 3.14 ships a wheel whose advertised floor never ran (issue #150).

Resolution, the project's own declaration first:

1. A pegged ``.python-version`` file.
2. The ``requires-python`` FLOOR from ``pyproject.toml``. The floor, not the
   newest version satisfying it: testing above the floor hides a 3.14-only
   feature reaching a repo that promises 3.12.
3. An explicitly requested version.
4. The fleet default.

uv honours (1) natively but resolves (2) to the NEWEST satisfying interpreter,
so the floor is computed here and handed to uv explicitly.

A version resolves to ``major.minor``: the patch is the runner's to choose, and
pinning it makes every upstream patch release a manifest edit.

Stdlib only, with no imports from the rest of the package, because the
``predict-version`` composite loads this file BY PATH where hyperi-ci is not
installed (as :mod:`hyperi_ci.version_source` is).
"""

# predict-version loads this file by path on the runner's python3, which may predate 3.14.
from __future__ import annotations

import re
import tomllib
from pathlib import Path

# Lower bounds only: `>=`, `~=` and `==`. `!=`, a bare `<` and `>3.11` (which
# excludes a version rather than naming the first allowed) are not read.
_LOWER_BOUND_RE = re.compile(r"(?:>=|~=|==)\s*(\d+)\.(\d+)")

# The first version-shaped token on the line, past any implementation prefix
# (`pypy@3.10`, `cpython-3.12`). The patch segment is optional.
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def _as_major_minor(major: str, minor: str) -> str:
    return f"{major}.{minor}"


def pegged_version(root: Path) -> str | None:
    """Return the version a ``.python-version`` file pegs, or None.

    Taken verbatim, even if the project does not otherwise support the version.
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

    The lowest of several lower bounds wins: the oldest interpreter promised.

    Args:
        spec: A PEP 440 specifier set, such as ``">=3.12,<4.0"``, as PyPI
            publishes per release file.

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

    The lowest of several lower bounds wins: the oldest interpreter promised,
    which a CI run has to prove.
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
            own declaration, or the floor stops being tested.
        default: The fleet default, used when the project declares nothing.

    Returns:
        ``(version, source)``. ``version`` is empty only when nothing declared
        one and no default was given, which the caller treats as "leave the
        interpreter choice alone".

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
