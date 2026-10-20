# Project:   HyperI CI
# File:      tests/unit/test_release_rules.py
# Purpose:   Tests for the commit-type -> bump SSoT (semantic-release defaults)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
import json
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.release_rules import (
    classify_commit,
    default_type_bump,
    releases,
)

# --- defaults (mirror semantic-release commit-analyzer default-release-rules) -


def test_default_map_is_semantic_release_defaults() -> None:
    # feat/minor, fix/perf/revert/patch - and NOTHING else. hotfix/sec/security
    # are deliberately absent (the collapse to pure defaults).
    assert default_type_bump() == {
        "feat": "minor",
        "fix": "patch",
        "perf": "patch",
    }


def test_default_map_is_a_copy() -> None:
    # Mutating the returned dict must not poison the module-level default.
    m = default_type_bump()
    m["feat"] = "major"
    assert default_type_bump()["feat"] == "minor"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("feat: add source", "minor"),
        ("fix: off-by-one", "patch"),
        ("perf: fewer allocs", "patch"),
        ("revert: undo change", "none"),  # bare revert: prefix (no body) -> none
        ("hotfix: prod incident", "none"),  # not a default rule
        ("security: patch CVE", "none"),  # not a default rule
        ("sec: patch CVE", "none"),  # not a default rule
        ("docs: readme", "none"),
        ("chore: deps", "none"),
        ("feat!: drop flag", "major"),
        ("fix(core)!: change return", "major"),
        ("fix: x\n\nBREAKING CHANGE: removed API", "major"),
        ("fix: x\n\nBREAKING-CHANGE: hyphenated", "major"),
        ("not conventional at all", "none"),
    ],
)
def test_classify_commit_defaults(message: str, expected: str) -> None:
    assert classify_commit(message) == expected


def test_releases_helper() -> None:
    assert releases("feat") is True
    assert releases("fix") is True
    assert releases("hotfix") is False
    assert releases("docs") is False


# --- drift guard on the central injected default ------------------------------


def _central_default() -> dict[str, Any]:
    """Load the config the setup-semantic-release composite injects."""
    repo_root = Path(__file__).resolve().parents[2]
    default_rc = (
        repo_root
        / ".github"
        / "actions"
        / "setup-semantic-release"
        / "default.releaserc.json"
    )
    return json.loads(default_rc.read_text(encoding="utf-8"))


def test_central_default_declares_only_main_and_beta_prerelease() -> None:
    """Pin the release branches -- main plus a beta prerelease, nothing else.

    Every repo without its own .releaserc inherits this file, so a branch
    added here starts cutting releases across the fleet with no opt-in.
    beta carries channel false: with no npm or github plugin loaded, a
    derived dist-tag does nothing. Issue #37 is why this config stays
    minimal -- the git/github plugins once destroyed every tag on
    dfe-receiver.
    """
    assert _central_default()["branches"] == [
        "main",
        {"name": "beta", "prerelease": True, "channel": False},
    ]


def test_central_default_carries_no_custom_release_rules() -> None:
    """The injected default MUST rely on semantic-release's own rules.

    If someone re-adds a hand-maintained releaseRules list to the central
    default.releaserc.json, the whole 'use semantic-release defaults' SSoT
    silently regresses. Pin it: the commit-analyzer plugin carries no
    releaseRules.
    """
    data = _central_default()
    analyzer = next(
        p
        for p in data["plugins"]
        if isinstance(p, list) and p[0] == "@semantic-release/commit-analyzer"
    )
    opts = analyzer[1] if len(analyzer) > 1 else {}
    assert "releaseRules" not in opts, (
        "central default must carry NO custom releaseRules - the bump is "
        "semantic-release's own defaults (see release_rules.py)"
    )
