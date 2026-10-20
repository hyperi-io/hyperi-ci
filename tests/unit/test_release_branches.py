# Project:   HyperI CI
# File:      tests/unit/test_release_branches.py
# Purpose:   Tests for prerelease branch declarations and version identity
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.release_branches` (issue #144).

Real config files in `tmp_path`, no mocks -- the module's whole job is
reading the files semantic-release will read.
"""

import json
from pathlib import Path

import pytest

from hyperi_ci.release_branches import (
    FLEET_PRERELEASE_BRANCHES,
    branch_from_ref,
    central_prerelease_branches,
    effective_release_channel,
    is_prerelease_ref,
    is_prerelease_version,
    prerelease_branch_names,
    prerelease_label,
)

CENTRAL_CONFIG = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "actions"
    / "setup-semantic-release"
    / "default.releaserc.json"
)


def _write_central(tmp_path: Path, branches: list) -> Path:
    path = tmp_path / "default.releaserc.json"
    path.write_text(json.dumps({"branches": branches}), encoding="utf-8", newline="\n")
    return path


class TestPrereleaseBranchNames:
    def test_the_shipped_central_config_declares_beta(self) -> None:
        # The fleet-wide declaration: every repo inheriting the central config
        # can cut a prerelease off `beta`.
        doc = json.loads(CENTRAL_CONFIG.read_text(encoding="utf-8"))
        assert prerelease_branch_names(doc) == ("beta",)

    def test_the_mirrored_fleet_default_matches_the_central_config(self) -> None:
        # The CLI answers from the constant because a consumer checkout has no
        # copy of the central file, so drift would let `push --publish` accept
        # a branch the CI gate then validates instead of releasing.
        doc = json.loads(CENTRAL_CONFIG.read_text(encoding="utf-8"))
        assert FLEET_PRERELEASE_BRANCHES == prerelease_branch_names(doc)

    def test_a_plain_string_entry_is_a_stable_branch(self) -> None:
        assert prerelease_branch_names({"branches": ["main", "master"]}) == ()

    def test_a_named_prerelease_label_still_counts(self) -> None:
        doc = {"branches": ["main", {"name": "next", "prerelease": "rc"}]}
        assert prerelease_branch_names(doc) == ("next",)

    def test_prerelease_false_is_not_a_prerelease_branch(self) -> None:
        doc = {"branches": [{"name": "main", "prerelease": False}]}
        assert prerelease_branch_names(doc) == ()

    def test_order_is_preserved_and_duplicates_collapse(self) -> None:
        doc = {
            "branches": [
                {"name": "beta", "prerelease": True},
                {"name": "alpha", "prerelease": True},
                {"name": "beta", "prerelease": True},
            ]
        }
        assert prerelease_branch_names(doc) == ("beta", "alpha")

    @pytest.mark.parametrize("doc", [None, [], "branches", {}, {"branches": "beta"}])
    def test_a_config_with_no_usable_branches_yields_none(self, doc: object) -> None:
        assert prerelease_branch_names(doc) == ()


class TestCentralPrereleaseBranches:
    def test_reads_the_central_config(self, tmp_path: Path) -> None:
        central = _write_central(
            tmp_path, ["main", {"name": "beta", "prerelease": True}]
        )
        assert central_prerelease_branches(central) == ("beta",)

    def test_the_shipped_config_matches_the_fleet_default(self) -> None:
        assert central_prerelease_branches(CENTRAL_CONFIG) == FLEET_PRERELEASE_BRANCHES

    def test_a_missing_central_config_declares_nothing(self, tmp_path: Path) -> None:
        assert central_prerelease_branches(tmp_path / "absent.json") == ()

    def test_an_unparseable_central_config_declares_nothing(
        self, tmp_path: Path
    ) -> None:
        # Closed direction: a config we cannot read must not open the release
        # gate on a branch semantic-release may reject.
        central = tmp_path / "default.releaserc.json"
        central.write_text("{not json", encoding="utf-8", newline="\n")
        assert central_prerelease_branches(central) == ()


class TestIsPrereleaseRef:
    def test_a_declared_branch_releases(self) -> None:
        assert is_prerelease_ref("refs/heads/beta", ("beta",)) is True

    def test_main_is_not_a_prerelease_branch(self) -> None:
        assert is_prerelease_ref("refs/heads/main", ("beta",)) is False

    def test_an_undeclared_branch_does_not_release(self) -> None:
        assert is_prerelease_ref("refs/heads/fix/thing", ("beta",)) is False

    def test_a_tag_ref_is_not_a_branch(self) -> None:
        assert is_prerelease_ref("refs/tags/v1.2.0", ("beta",)) is False

    def test_a_slashed_branch_name_matches_whole(self) -> None:
        assert is_prerelease_ref("refs/heads/release/beta", ("release/beta",)) is True

    def test_no_declared_branches_means_no_release(self) -> None:
        assert is_prerelease_ref("refs/heads/beta", ()) is False


class TestBranchFromRef:
    def test_strips_the_heads_prefix(self) -> None:
        assert branch_from_ref("refs/heads/beta") == "beta"

    def test_a_non_branch_ref_has_no_branch(self) -> None:
        assert branch_from_ref("refs/tags/v1.0.0") == ""


class TestVersionIdentity:
    @pytest.mark.parametrize(
        ("version", "label"),
        [
            ("1.2.0-beta.1", "beta"),
            ("v1.2.0-beta.1", "beta"),
            ("1.2.0-rc.1", "rc"),
            ("2.0.0-alpha.12", "alpha"),
            ("1.2.0-beta.1+abc123", "beta"),
            ("1.2.0", None),
            ("v1.2.0", None),
            ("", None),
            (None, None),
            ("not-a-version", None),
        ],
    )
    def test_label_reads_the_prerelease_component(
        self, version: str | None, label: str | None
    ) -> None:
        assert prerelease_label(version) == label

    def test_is_prerelease_version_agrees_with_the_label(self) -> None:
        assert is_prerelease_version("1.2.0-beta.1") is True
        assert is_prerelease_version("1.2.0") is False


class TestEffectiveReleaseChannel:
    def test_a_stable_version_keeps_the_configured_channel(self) -> None:
        assert effective_release_channel("release", "1.2.0") == "release"

    def test_a_prerelease_version_never_ships_as_release(self) -> None:
        # The GA `latest/` path and the `--prerelease` flag both hang off this.
        assert effective_release_channel("release", "1.2.0-beta.1") == "beta"

    def test_the_version_label_wins_over_a_pre_ga_config(self) -> None:
        assert effective_release_channel("alpha", "1.2.0-rc.1") == "rc"

    def test_an_unreadable_version_leaves_the_config_alone(self) -> None:
        assert effective_release_channel("release", None) == "release"
