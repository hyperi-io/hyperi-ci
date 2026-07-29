# Project:   HyperI CI
# File:      src/hyperi_ci/deps/versions.py
# Purpose:   Constraint floors and version comparison for the drift audit
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A deliberately small version parser for the floor-vs-lock comparison.

This is a WARNING GENERATOR, not a resolver. It only has to answer "is the lock
a major ahead of what the manifest admits to", and nothing finer. Pre-release
suffixes, epochs, local versions and build metadata are all ignored, because
none of them change that answer. No `packaging` dependency: the estate
exact-pins its deps, so a new one carries fleet-wide knock-on to buy precision
this comparison never uses.

NOT the same job as ``scripts/update-versions.py``'s ``_parse_semver``, and
deliberately not shared with it. That one must REJECT a suffixed tag
(``v3.1.0-node20`` is a backport that must never win release selection); this
one must ACCEPT ``1.2.3rc1`` so a floor can still be compared. Merging them
would break whichever side lost.
"""

from __future__ import annotations

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
    """Lowest version a constraint admits, spelled as it was written.

    Handles the shapes that actually appear: ``>=X.Y.Z``, ``>=X,<Y``, ``~=X.Y``,
    ``^X.Y`` / ``~X.Y`` (cargo, npm), ``==X.Y.Z`` / ``=X`` (exact), ``>X``, and
    a bare ``X.Y.Z``. Anything with no lower bound at all -- ``*``, ``<2``,
    ``workspace:*``, a git or path dependency -- returns None and is skipped.
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

    A 0.x floor gets the minor check as well, because 0.x treats minor as the
    breaking axis (semver section 4) -- ``>=0.23`` against a locked 0.40 is the
    same class of staleness as ``>=1`` against a locked 2. That mirrors the
    clamp table in docs/dependencies/deps-pinning.md.
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
    """cargo treats ``-`` and ``_`` as the same character in a crate name."""
    return str(name).replace("_", "-").lower()


def norm_npm(name: str) -> str:
    """npm names are case-insensitive in practice; fold for lookup."""
    return str(name).lower()
