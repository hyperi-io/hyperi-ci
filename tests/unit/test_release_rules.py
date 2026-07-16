# Project:   HyperI CI
# File:      tests/unit/test_release_rules.py
# Purpose:   Tests for the commit-type -> bump SSoT (semantic-release defaults)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperi_ci.release_rules import (
    classify_commit,
    default_type_bump,
    load_type_bump,
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


# --- repo .releaserc.json override (the rare 0.01% exception) ------------------


def _write_releaserc(project_dir: Path, release_rules: list[dict]) -> None:
    (project_dir / ".releaserc.json").write_text(
        json.dumps(
            {
                "branches": ["main"],
                "plugins": [
                    [
                        "@semantic-release/commit-analyzer",
                        {
                            "preset": "conventionalcommits",
                            "releaseRules": release_rules,
                        },
                    ],
                    "@semantic-release/release-notes-generator",
                ],
            }
        ),
        encoding="utf-8",
    )


def test_no_releaserc_uses_defaults(tmp_path: Path) -> None:
    assert load_type_bump(tmp_path) == default_type_bump()


def test_releaserc_override_adds_and_overrides(tmp_path: Path) -> None:
    _write_releaserc(
        tmp_path,
        [
            {"type": "hotfix", "release": "patch"},  # add a type
            {"type": "feat", "release": False},  # override a default OFF
            {"type": "docs", "release": "minor"},  # promote a no-release type
        ],
    )
    mapping = load_type_bump(tmp_path)
    assert mapping["hotfix"] == "patch"  # repo added it
    assert mapping["feat"] == "none"  # repo turned the default off
    assert mapping["docs"] == "minor"  # repo promoted it
    assert mapping["fix"] == "patch"  # untouched default retained
    # classify_commit honours the override map
    assert classify_commit("hotfix: prod", mapping) == "patch"
    assert classify_commit("feat: thing", mapping) == "none"


def test_malformed_releaserc_falls_back_to_defaults(tmp_path: Path) -> None:
    (tmp_path / ".releaserc.json").write_text("{ not valid json", encoding="utf-8")
    assert load_type_bump(tmp_path) == default_type_bump()


@pytest.mark.parametrize("body", ["null", "[]", '"a string"', "123"])
def test_non_object_releaserc_falls_back_to_defaults(tmp_path: Path, body: str) -> None:
    # Valid JSON but not an object (a bare list / null / scalar) must NOT crash
    # on .get() - it falls back to defaults, so `hyperi-ci push` stays alive.
    (tmp_path / ".releaserc.json").write_text(body, encoding="utf-8")
    assert load_type_bump(tmp_path) == default_type_bump()


def test_releaserc_without_analyzer_block_uses_defaults(tmp_path: Path) -> None:
    (tmp_path / ".releaserc.json").write_text(
        json.dumps({"branches": ["main"], "plugins": ["@semantic-release/github"]}),
        encoding="utf-8",
    )
    assert load_type_bump(tmp_path) == default_type_bump()


# --- drift guard on the central injected default ------------------------------


def test_central_default_carries_no_custom_release_rules() -> None:
    """The injected default MUST rely on semantic-release's own rules.

    If someone re-adds a hand-maintained releaseRules list to the central
    default.releaserc.json, the whole 'use semantic-release defaults' SSoT
    silently regresses. Pin it: the commit-analyzer plugin carries no
    releaseRules.
    """
    repo_root = Path(__file__).resolve().parents[2]
    default_rc = (
        repo_root
        / ".github"
        / "actions"
        / "setup-semantic-release"
        / "default.releaserc.json"
    )
    data = json.loads(default_rc.read_text(encoding="utf-8"))
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
