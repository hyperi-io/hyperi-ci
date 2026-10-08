# Project:   HyperI CI
# File:      src/hyperi_ci/deps/versions.py
# Purpose:   Constraint floors and version comparison for the drift audit
# Origin:    Derek's deps automation scripts, merged into hyperi-ci now they are
#            mature enough for people (and hyperi-ai's /deps) to use directly
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A small version parser for the floor-vs-lock warning, not a resolver.

It only asks whether the lock is a major (or a 0.x minor) ahead of the floor,
so pre-release, epoch, local and build parts are ignored and ``packaging`` is
not needed. Do not share it with ``scripts/update-versions.py``'s
``_parse_semver``: that must reject ``v3.1.0-node20``, and this must accept
``1.2.3rc1``.
"""

import re

_VERSION_HEAD = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_FLOOR_GE = re.compile(r">=\s*v?(\d+(?:\.\d+)*)")
_FLOOR_COMPAT = re.compile(r"~=\s*v?(\d+(?:\.\d+)*)")
_FLOOR_CARET = re.compile(r"[\^~]\s*v?(\d+(?:\.\d+)*)")
# `(?<![<>!])` keeps `<=2` and `!=1.0` out: neither declares a floor.
_FLOOR_EQ = re.compile(r"(?<![<>!])=+\s*v?(\d+(?:\.\d+)*)")
_FLOOR_GT = re.compile(r">\s*v?(\d+(?:\.\d+)*)")
_FLOOR_BARE = re.compile(r"^v?(\d+(?:\.\d+)*)")

_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?P<spec>.*)$"
)


def parse(text: str) -> tuple[int, int, int] | None:
    """``"1.2.3rc1"`` -> ``(1, 2, 3)``. None when there is no leading number."""
    match = _VERSION_HEAD.match(str(text).strip().lstrip("vV"))
    if match is None:
        return None
    parts = [int(g) if g else 0 for g in match.groups()]
    return (parts[0], parts[1], parts[2])


def floor_of(constraint: str) -> str | None:
    """Return the lowest version a constraint admits, spelled as written.

    Handles ``>=X``, ``~=X``, ``^X``, ``~X``, ``==X``, ``=X``, ``>X`` and a bare
    ``X``. A constraint with no lower bound (``*``, ``<2``, ``workspace:*``, a
    git or path dependency) returns None.
    """
    head = str(constraint).split(";", 1)[0].strip().strip("\"'")
    if not head or head in ("*", "latest"):
        return None
    for pattern in (_FLOOR_GE, _FLOOR_COMPAT, _FLOOR_CARET, _FLOOR_EQ, _FLOOR_GT):
        match = pattern.search(head)
        if match is not None:
            return match.group(1)
    match = _FLOOR_BARE.match(head)
    return match.group(1) if match is not None else None


def drift_kind(floor: str, locked: str) -> str | None:
    """``"major"``, ``"minor"``, or None when the floor still covers the lock.

    A 0.x floor also gets the minor check, since minor is 0.x's breaking axis
    (semver section 4, the clamp table in docs/dependencies/deps-pinning.md).
    """
    low = parse(floor)
    high = parse(locked)
    if low is None or high is None:
        return None
    if high[0] > low[0]:
        return "major"
    if low[0] == 0 and high[1] > low[1]:
        return "minor"
    return None


def split_requirement(text: str) -> tuple[str, str]:
    """``"moto[secretsmanager]>=5.2.0"`` -> ``("moto", ">=5.2.0")``."""
    head = str(text).split(";", 1)[0].strip()
    if not head or head[0] in "-#":
        return "", ""
    match = _REQUIREMENT.match(head)
    if match is None:
        return "", ""
    return match.group("name"), match.group("spec").strip()


def norm_python(name: str) -> str:
    """PEP 503 normalisation, so ``Pytest_AsyncIO`` finds ``pytest-asyncio``."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def norm_cargo(name: str) -> str:
    """Cargo treats ``-`` and ``_`` as the same character in a crate name."""
    return str(name).replace("_", "-").lower()


def norm_npm(name: str) -> str:
    """Npm names are case-insensitive in practice; fold for lookup."""
    return str(name).lower()
