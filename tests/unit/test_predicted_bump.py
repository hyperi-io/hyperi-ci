# Project:   HyperI CI
# File:      tests/unit/test_predicted_bump.py
# Purpose:   Tests for the predicted-bump gate (issue #26)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyperi_ci.quality.predicted_bump import (
    classify_commit,
    predict_bump,
)

# --- classify_commit (pure) ----------------------------------------------
#
# The bump map is semantic-release's own default-release-rules (mirrored in
# hyperi_ci.release_rules). classify_commit() with no explicit map uses those
# defaults - so `security`/`hotfix` are NO LONGER release-worthy on their own
# (that was the deliberate collapse to pure semantic-release defaults).


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("fix: correct off-by-one", "patch"),
        ("perf: avoid alloc in loop", "patch"),
        ("revert: undo the thing", "none"),  # bare revert: prefix -> no release
        ("security: patch CVE", "none"),  # not a default rule -> no release
        ("hotfix: prod incident", "none"),  # not a default rule -> no release
        ("feat: add new source", "minor"),
        ("feat(api): add endpoint", "minor"),
        ("feat!: drop legacy flag", "major"),
        ("fix(core)!: change return type", "major"),
        ("docs: tidy readme", "none"),
        ("chore: bump deps", "none"),
        ("refactor: extract helper", "none"),
        ("not a conventional commit", "none"),
        ("feat: add thing\n\nBREAKING CHANGE: removes old API", "major"),
        ("fix: thing\n\nBREAKING-CHANGE: hyphenated form", "major"),
    ],
)
def test_classify_commit(message: str, expected: str) -> None:
    assert classify_commit(message) == expected


# --- predict_bump (real git) ---------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.io")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "commit", "--allow-empty", "-m", "chore: seed")
    return tmp_path


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "commit", "--allow-empty", "-m", message)


def test_no_tag_returns_none(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "feat!: massive change")
    pred = predict_bump(tmp_path)
    # No prior tag => initial release => gate must not fire.
    assert pred.bump == "none"
    assert pred.last_tag is None


def test_patch_only_range(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "tag", "v1.0.0")
    _commit(tmp_path, "fix: a")
    _commit(tmp_path, "docs: b")
    pred = predict_bump(tmp_path)
    assert pred.bump == "patch"
    assert pred.last_tag == "v1.0.0"


def test_feat_predicts_minor(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "tag", "v1.0.0")
    _commit(tmp_path, "fix: a")
    _commit(tmp_path, "feat: new thing")
    pred = predict_bump(tmp_path)
    assert pred.bump == "minor"
    assert "feat: new thing" in pred.minor_reasons


def _current_branch(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_merge_imported_breaking_predicts_major(tmp_path: Path) -> None:
    # The issue #26 scenario: a reconcile merge brings old breaking history
    # into reachability even though HEAD's own subject is a clean fix:.
    _init_repo(tmp_path)
    _git(tmp_path, "tag", "v1.0.0")
    base = _current_branch(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "side")
    _commit(tmp_path, "feat!: remove legacy API")
    _git(tmp_path, "checkout", "-q", base)
    _commit(tmp_path, "fix: unrelated")
    _git(tmp_path, "merge", "--no-ff", "-m", "fix: reconcile side", "side")
    pred = predict_bump(tmp_path)
    assert pred.bump == "major"
    assert any("remove legacy API" in r for r in pred.major_reasons)


def test_non_semver_v_tag_does_not_poison_range(tmp_path: Path) -> None:
    # A non-semver v-tag (vendor-x) sorts ABOVE vX.Y.Z under -v:refname.
    # The gate must skip it and walk from the real release tag, else a
    # feat!/BREAKING commit since v1.2.3 goes unanalysed (review finding
    # on issue #26).
    _init_repo(tmp_path)
    _git(tmp_path, "tag", "v1.2.3")
    _commit(tmp_path, "feat!: drop legacy API")
    _git(tmp_path, "tag", "vendor-x")
    _commit(tmp_path, "fix: unrelated")
    pred = predict_bump(tmp_path)
    assert pred.last_tag == "v1.2.3"
    assert pred.bump == "major"


def test_prerelease_v_tag_skipped(tmp_path: Path) -> None:
    # Prereleases are not final-release anchors; the gate bumps from the
    # last FINAL tag, matching the composite's v[0-9]* + X.Y.Z discipline.
    _init_repo(tmp_path)
    _git(tmp_path, "tag", "v1.2.3")
    _commit(tmp_path, "fix: a")
    _git(tmp_path, "tag", "v1.3.0-rc.1")
    _commit(tmp_path, "fix: b")
    pred = predict_bump(tmp_path)
    assert pred.last_tag == "v1.2.3"
