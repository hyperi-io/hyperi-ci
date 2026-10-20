# Project:   HyperI CI
# File:      src/hyperi_ci/release_rules.py
# Purpose:   Commit-type -> version-bump SSoT (semantic-release defaults)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve the version bump a commit type implies.

The source of truth is semantic-release's own default-release-rules
(`@semantic-release/commit-analyzer/lib/default-release-rules.js`). There is no
per-repo override: CI tags every repo with the central config, so a repo's own
release config would only make this prediction disagree with the tag.

The pre-push gate (`hyperi-ci push`) and the commit-msg hook mirror the rules
so they predict semantic-release's bump without a Node install. When git is
unavailable or there is no prior tag the caller treats the result as "no
prediction" and fails open - see :mod:`hyperi_ci.quality.predicted_bump`.

The default rules (commit-analyzer, semantic-release 25):

    breaking (``!`` or ``BREAKING CHANGE:`` footer)  -> major
    feat                                             -> minor
    fix                                              -> patch
    perf                                             -> patch
    everything else                                  -> no release

``hotfix`` / ``sec`` / ``security`` do not release on their own: ship a
security patch as ``fix(security): ...``.

``revert`` is left out of the map. semantic-release patch-bumps a revert by the
parser's flag (a body carrying ``This reverts commit <sha>``), not the type
prefix, and a header-only mirror cannot see that. The pre-push gate only blocks
minor/major, so the under-count is harmless.
"""

# predict-version loads this file by path on the runner's python3, which may predate 3.14.
from __future__ import annotations

import re

# Bump precedence, low -> high.
_BUMP_ORDER = {"none": 0, "patch": 1, "minor": 2, "major": 3}

# Mirror of @semantic-release/commit-analyzer default-release-rules.js
# (semantic-release 25). `breaking` is handled structurally in
# classify_commit (a `!` marker or a BREAKING CHANGE footer beats any type).
_SEMREL_DEFAULT_BUMP: dict[str, str] = {
    "feat": "minor",
    "fix": "patch",
    "perf": "patch",
}

# `type: subject` / `type(scope): subject`, optionally with a `!` breaking
# marker before the colon (`feat!:`, `fix(api)!:`).
_HEADER_RE = re.compile(r"^(?P<type>[a-z]+)(?:\([a-z0-9._/-]+\))?(?P<bang>!)?:")
_BREAKING_CHANGE_RE = re.compile(r"BREAKING[ \-]CHANGE:")


def default_type_bump() -> dict[str, str]:
    """Return a fresh copy of the semantic-release default type->bump map."""
    return dict(_SEMREL_DEFAULT_BUMP)


def classify_commit(message: str, type_bump: dict[str, str] | None = None) -> str:
    """Return the bump a single commit implies.

    ``none`` / ``patch`` / ``minor`` / ``major``. A breaking marker (``!`` on
    any type, or a ``BREAKING CHANGE:`` footer) is a major regardless of type.
    """
    if type_bump is None:
        type_bump = _SEMREL_DEFAULT_BUMP
    if _BREAKING_CHANGE_RE.search(message):
        return "major"
    header = message.splitlines()[0].strip() if message else ""
    m = _HEADER_RE.match(header)
    if not m:
        return "none"
    if m.group("bang"):
        return "major"
    return type_bump.get(m.group("type"), "none")


def releases(commit_type: str, type_bump: dict[str, str] | None = None) -> bool:
    """Return True when ``commit_type`` alone triggers a release (bump != none)."""
    if type_bump is None:
        type_bump = _SEMREL_DEFAULT_BUMP
    return type_bump.get(commit_type, "none") != "none"
