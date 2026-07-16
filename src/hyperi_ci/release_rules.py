# Project:   HyperI CI
# File:      src/hyperi_ci/release_rules.py
# Purpose:   Commit-type -> version-bump SSoT (semantic-release defaults)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve the version bump a commit type implies.

Single source of truth is **semantic-release's own default-release-rules**
(`@semantic-release/commit-analyzer/lib/default-release-rules.js`), NOT a
HyperI-maintained taxonomy. A repo overrides only by shipping its own
`.releaserc.json` with a `commit-analyzer` `releaseRules` block -- the rare
exception (e.g. a multi-crate workspace). 99.99% of repos carry no `.releaserc`
and resolve straight to the defaults below.

Why mirror the rules in Python at all: the pre-push gate (`hyperi-ci push`)
and the commit-msg hook must predict the same bump semantic-release will cut,
WITHOUT a Node / semantic-release install in the loop. When git is unavailable
or there is no prior tag the caller treats the result as "no prediction" and
fails open -- see :mod:`hyperi_ci.quality.predicted_bump`.

The default rules (commit-analyzer, semantic-release 25):

    breaking (``!`` or ``BREAKING CHANGE:`` footer)  -> major
    feat                                             -> minor
    fix                                              -> patch
    perf                                             -> patch
    everything else                                  -> no release

That is the WHOLE taxonomy. Types HyperI once patch-bumped by hand
(``hotfix`` / ``sec`` / ``security``) are no longer release-worthy on their
own -- ship a security patch as ``fix(security): ...`` (a real ``fix`` that
bumps), or add a repo ``.releaserc.json`` override.

Note on revert: semantic-release ALSO patch-bumps a genuine revert, but its
``{revert: true}`` rule keys on the PARSER's revert flag -- a commit whose
body carries ``This reverts commit <sha>`` -- NOT the ``revert:`` type prefix.
A header-only mirror cannot detect that reliably, so ``revert`` is left out of
the map (a bare ``revert:`` resolves to no-release, matching semantic-release
for the no-body case). The pre-push gate only blocks minor/major, so
under-counting a revert patch is harmless.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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

# A repo override may express "no release" as `false` or `null`.
_NO_RELEASE = (False, None, "false", "none")


def default_type_bump() -> dict[str, str]:
    """Return a fresh copy of the semantic-release default type->bump map."""
    return dict(_SEMREL_DEFAULT_BUMP)


def _releaserc_overrides(project_dir: Path) -> dict[str, str]:
    """Read a repo ``.releaserc.json`` commit-analyzer ``releaseRules``.

    Returns a ``type -> bump`` map for whatever the repo declares (the rare
    exception). Missing file / unparseable / no analyzer block -> empty map,
    so the defaults stand. Only ``.releaserc.json`` is honoured: YAML
    ``.releaserc`` is deprecated (see config/deprecated-files.yaml).
    """
    rc = project_dir / ".releaserc.json"
    if not rc.exists():
        return {}
    try:
        data = json.loads(rc.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    overrides: dict[str, str] = {}
    for plugin in data.get("plugins", []) or []:
        if not (
            isinstance(plugin, list)
            and plugin
            and plugin[0] == "@semantic-release/commit-analyzer"
        ):
            continue
        opts = plugin[1] if len(plugin) > 1 and isinstance(plugin[1], dict) else {}
        for rule in opts.get("releaseRules", []) or []:
            if not isinstance(rule, dict):
                continue
            rule_type = rule.get("type")
            if rule_type is None:
                continue
            release = rule.get("release")
            if release in _BUMP_ORDER:
                overrides[str(rule_type)] = str(release)
            elif release in _NO_RELEASE:
                overrides[str(rule_type)] = "none"
    return overrides


def load_type_bump(project_dir: Path | None = None) -> dict[str, str]:
    """Build the effective ``type -> bump`` map for ``project_dir``.

    semantic-release defaults, overlaid with a repo ``.releaserc.json``
    override when one is present. ``project_dir=None`` -> defaults only.
    """
    mapping = default_type_bump()
    if project_dir is not None:
        mapping.update(_releaserc_overrides(project_dir))
    return mapping


def classify_commit(message: str, type_bump: dict[str, str] | None = None) -> str:
    """Return the bump a single commit implies.

    ``none`` / ``patch`` / ``minor`` / ``major``. A breaking marker (a ``!``
    on any type, or a ``BREAKING CHANGE:`` footer) is a major regardless of
    type -- exactly what semantic-release's commit-analyzer does.
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
