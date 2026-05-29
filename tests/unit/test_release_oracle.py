# Project:   HyperI CI
# File:      tests/unit/test_release_oracle.py
# Purpose:   Tests for the release-based version oracle (#31 Phase 2a)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Version oracle: next-version from the last GitHub release + conventional
commit analysis — replacing semantic-release's git-tag-reachability dry-run so
release tags can live off-main (frozen graph, #31) and orphaned tags can't
break version computation."""

from __future__ import annotations

import json
from pathlib import Path

from hyperi_ci.release.oracle import (
    RELEASE_RULES,
    bump_version,
    commit_bump,
    compute_next_version,
    highest_release,
    max_bump,
    parse_semver,
)


class TestVersionSource:
    """Highest pure-semver tag across ALL tags — not reachability-bound."""

    def test_parse_semver(self) -> None:
        assert parse_semver("v1.2.3") == (1, 2, 3)
        assert parse_semver("1.2.3") == (1, 2, 3)
        assert parse_semver("v1.2.3-dev.9") is None  # prerelease excluded
        assert parse_semver("nightly") is None

    def test_highest_release_ignores_prereleases_and_order(self) -> None:
        tags = ["v1.4.2", "v2.4.2", "v1.2.0-dev.9", "v2.4.1", "v10.0.0-rc1"]
        assert highest_release(tags) == "2.4.2"

    def test_highest_release_none_when_no_semver(self) -> None:
        assert highest_release(["nightly", "latest"]) is None

    def test_highest_release_counts_all_tags(self) -> None:
        # An orphaned/off-main tag is still the highest → next exceeds it
        # (orphan-immune; enables off-main frozen tags).
        assert highest_release(["v1.0.0", "v9.9.9", "v1.0.1"]) == "9.9.9"


class TestCommitBump:
    def test_feat_minor(self) -> None:
        assert commit_bump("feat: add thing") == "minor"

    def test_fix_patch(self) -> None:
        assert commit_bump("fix: a bug") == "patch"

    def test_perf_and_sec_patch(self) -> None:
        assert commit_bump("perf: faster") == "patch"
        assert commit_bump("sec: harden") == "patch"

    def test_docs_chore_none(self) -> None:
        assert commit_bump("docs: x") is None
        assert commit_bump("chore: x") is None

    def test_scope(self) -> None:
        assert commit_bump("fix(api): x") == "patch"

    def test_bang_is_major(self) -> None:
        assert commit_bump("feat!: x") == "major"
        assert commit_bump("feat(api)!: x") == "major"

    def test_breaking_footer_is_major(self) -> None:
        assert commit_bump("feat: x\n\nBREAKING CHANGE: gone") == "major"
        assert commit_bump("fix: x\n\nBREAKING-CHANGE: gone") == "major"

    def test_unknown_and_nonconventional_none(self) -> None:
        assert commit_bump("wip: x") is None  # our rules: wip → none
        assert commit_bump("revert: x") is None  # override (preset would patch)
        assert commit_bump("Merge branch 'main'") is None


class TestMaxBump:
    def test_highest_wins(self) -> None:
        assert max_bump(["patch", "minor", None]) == "minor"
        assert max_bump(["patch", "major", "minor"]) == "major"

    def test_all_none_and_empty(self) -> None:
        assert max_bump([None, None]) is None
        assert max_bump([]) is None


class TestBumpVersion:
    def test_bumps(self) -> None:
        assert bump_version("1.2.3", "major") == "2.0.0"
        assert bump_version("1.2.3", "minor") == "1.3.0"
        assert bump_version("1.2.3", "patch") == "1.2.4"

    def test_strips_leading_v(self) -> None:
        assert bump_version("v1.2.3", "patch") == "1.2.4"


class TestComputeNextVersion:
    def test_picks_highest_then_bumps(self) -> None:
        assert (
            compute_next_version("1.2.3", ["feat: a", "fix: b", "docs: c"]) == "1.3.0"
        )

    def test_no_release_worthy_returns_none(self) -> None:
        assert compute_next_version("1.2.3", ["docs: a", "chore: b"]) is None

    def test_first_release_is_1_0_0(self) -> None:
        assert compute_next_version(None, ["feat: a"]) == "1.0.0"
        assert compute_next_version(None, ["docs: a"]) is None


class TestRulesMatchConfig:
    """Anti-drift: the Python rules must mirror default.releaserc.json while
    semantic-release coexists (retired in #31 Phase 2d)."""

    def test_python_rules_equal_json(self) -> None:
        cfg = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / ".github/actions/setup-semantic-release/default.releaserc.json"
            ).read_text()
        )
        json_rules = cfg["plugins"][0][1]["releaseRules"]
        expected = {}
        for r in json_rules:
            if r.get("breaking"):
                continue  # breaking handled by commit_bump, not the type map
            expected[r["type"]] = r["release"] or None
        assert RELEASE_RULES == expected
