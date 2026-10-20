# Project:   HyperI CI
# File:      src/hyperi_ci/release_branches.py
# Purpose:   Prerelease branch declarations and version identity
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Which branches release, and whether a version consumes a stable number.

Two independent questions:

- **Version identity.** Does this run consume a stable version? A push to
  ``main`` cuts ``1.2.0``, and a push to a branch the release config declares
  ``prerelease`` cuts ``1.2.0-beta.1``. The ``branches`` array in the release
  config is the declaration, and semantic-release owns the mechanism.
- **Optimisation tier.** How hard the build optimises. Rust reads it from
  ``HYPERCI_CHANNEL`` (``languages/rust/optimize.py``), which the reusable
  workflow sets for any run that ships.

Any combination is legitimate, so this module supplies only the identity half.
Identity is read off the version string (``common.resolve_release_version``)
because a separate signal could disagree with it.
"""

# predict-version loads this file by path on the runner's python3, which may predate 3.14.
from __future__ import annotations

import json
import re
from pathlib import Path

# Semver core plus a prerelease component: 1.2.0-beta.1 -> label "beta".
# Build metadata (+sha) is not part of the label.
_PRERELEASE_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+-(?P<label>[0-9A-Za-z-]+)(?:\.[0-9A-Za-z-]+)*(?:\+.*)?$"
)

# Mirror of the `branches` array in
# .github/actions/setup-semantic-release/default.releaserc.json, which consumer
# checkouts lack. tests/unit/test_release_branches.py fails on drift.
FLEET_PRERELEASE_BRANCHES: tuple[str, ...] = ("beta",)


def prerelease_branch_names(doc: object) -> tuple[str, ...]:
    """Return the branch names a release config declares as prereleases.

    Both ``"prerelease": true`` and ``"prerelease": "rc"`` count. A plain string
    entry (``"main"``) is a stable branch and is never returned. A glob entry is
    returned verbatim and matched literally, so it never matches a real ref and
    the run validates rather than releases.

    Args:
        doc: Parsed release config, or any value when the parse failed.

    Returns:
        Declared prerelease branch names, in config order.

    """
    branches = doc.get("branches") if isinstance(doc, dict) else None
    if not isinstance(branches, list):
        return ()
    names: list[str] = []
    for entry in branches:
        if not isinstance(entry, dict) or not entry.get("prerelease"):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return tuple(names)


def central_prerelease_branches(central: Path) -> tuple[str, ...]:
    """Return the prerelease branches the central release config declares.

    For the CI gate, which can read the central config file itself. A repo's
    own release config is never loaded, so it never declares a branch.

    Args:
        central: Path to the central ``default.releaserc.json``.

    Returns:
        Declared prerelease branch names, empty when the file is unreadable.

    """
    try:
        return prerelease_branch_names(
            json.loads(central.read_text(encoding="utf-8", errors="replace"))
        )
    except (OSError, json.JSONDecodeError):
        return ()


def branch_from_ref(ref: str) -> str:
    """Return the branch name from a ``refs/heads/...`` ref, else ``""``."""
    prefix = "refs/heads/"
    return ref[len(prefix) :] if ref.startswith(prefix) else ""


def is_prerelease_ref(ref: str, prerelease_branches: tuple[str, ...]) -> bool:
    """Report whether ``ref`` names a declared prerelease branch.

    Args:
        ref: A GitHub ``GITHUB_REF`` value.
        prerelease_branches: Names from :func:`central_prerelease_branches`.

    Returns:
        True when the ref is a branch declared as a prerelease.

    """
    branch = branch_from_ref(ref)
    return bool(branch) and branch in prerelease_branches


def is_prerelease_version(version: str | None) -> bool:
    """Report whether ``version`` carries a semver prerelease component."""
    return prerelease_label(version) is not None


def prerelease_label(version: str | None) -> str | None:
    """Return the first prerelease identifier of ``version``, else None.

    ``1.2.0-beta.1`` yields ``beta``. A stable version or a non-semver string
    yields None.
    """
    if not version:
        return None
    match = _PRERELEASE_PATTERN.match(version.strip().removeprefix("v"))
    return match.group("label") if match else None


def effective_release_channel(configured: str, version: str | None) -> str:
    """Return the channel a version actually ships on.

    A prerelease version takes the channel its own label names, so the GitHub
    Release is marked prerelease and the R2 upload goes to that channel's
    prefix. Otherwise ``1.2.0-beta.1`` would overwrite the GA ``latest/`` path.

    Args:
        configured: ``release.channel`` as the project declares it.
        version: Version being released, with or without a leading ``v``.

    Returns:
        The channel to publish under.

    """
    label = prerelease_label(version)
    return label if label is not None else configured
