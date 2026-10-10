# Project:   HyperI CI
# File:      tests/unit/test_fork_readers.py
# Purpose:   Tests for fork-aware bump readers (predicted bump, forced bump, unreleased)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A fork's sync merge brings upstream ``feat:``, ``feat!:`` and a ``v9.0.0`` tag.

Each reader that once walked every parent must answer from the fork's own
first-parent history instead. Non-fork behaviour on the same history is the
control.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from hyperi_ci.commit_range import unreleased_since_tag, unreleased_warning
from hyperi_ci.common import run_cmd
from hyperi_ci.push import _compute_next_version
from hyperi_ci.quality.predicted_bump import predict_bump


def _declare(repo: Path, classification: str) -> None:
    (repo / ".hyperi-ci.yaml").write_text(
        f"classification: {classification}\n", encoding="utf-8"
    )


def _drop_upstream_tag(repo: Path) -> None:
    """Remove v9.0.0 so upstream's commits sit past the highest remaining tag."""
    run_cmd(["git", "tag", "-d", "v9.0.0"], capture=True, check=True, cwd=repo)


class TestPredictedBump:
    def test_fork_sync_merge_is_a_patch(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        _declare(repo, "fork")
        prediction = predict_bump(repo)
        assert prediction.bump == "patch"
        assert prediction.last_tag == "v0.2.8"
        assert prediction.major_reasons == []
        assert prediction.minor_reasons == []

    def test_fork_sync_merge_is_a_patch_without_the_upstream_tag(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        _drop_upstream_tag(repo)
        _declare(repo, "fork")
        prediction = predict_bump(repo)
        assert prediction.bump == "patch"
        assert prediction.major_reasons == []

    def test_fork_own_feat_is_still_a_minor(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path, own=("feat: own feature",))
        _declare(repo, "fork")
        prediction = predict_bump(repo)
        assert prediction.bump == "minor"
        assert prediction.minor_reasons == ["feat: own feature"]

    def test_fork_off_main_is_gated_like_the_release_that_runs_there(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        """A prerelease branch keeps semantic-release, so the gate counts every parent."""
        repo = make_fork_history(tmp_path)
        _drop_upstream_tag(repo)
        _declare(repo, "fork")
        run_cmd(
            ["git", "checkout", "-q", "-b", "beta"], capture=True, check=True, cwd=repo
        )
        assert predict_bump(repo).bump == "major"

    def test_non_fork_still_counts_upstream(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        _drop_upstream_tag(repo)
        _declare(repo, "hyperi")
        assert predict_bump(repo).bump == "major"

    def test_non_fork_takes_the_highest_tag(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        _declare(repo, "hyperi")
        assert predict_bump(repo).last_tag == "v9.0.0"


class TestForcedBump:
    def test_fork_bumps_from_its_own_tag(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        _declare(repo, "fork")
        assert _compute_next_version(bump="patch", cwd=str(repo)) == "0.2.9"
        assert _compute_next_version(bump="minor", cwd=str(repo)) == "0.3.0"

    def test_non_fork_bumps_from_the_highest_tag(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        _declare(repo, "hyperi")
        assert _compute_next_version(bump="patch", cwd=str(repo)) == "9.0.1"


class TestUnreleased:
    def test_fork_counts_only_its_own_commits(
        self,
        tmp_path: Path,
        make_fork_history: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_fork_history(tmp_path)
        _declare(repo, "fork")
        monkeypatch.chdir(repo)
        tag, releasable = unreleased_since_tag(repo)
        assert tag == "v0.2.8"
        # The fork's own fix and the sync merge, read by its subject.
        assert [bump for _sha, bump in releasable] == ["patch", "patch"]

    def test_fork_warning_counts_no_upstream_feature(
        self,
        tmp_path: Path,
        make_fork_history: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_fork_history(tmp_path)
        _drop_upstream_tag(repo)
        _declare(repo, "fork")
        monkeypatch.chdir(repo)
        warn, message = unreleased_warning(repo)
        assert warn is True
        assert "since v0.2.8" in message
        assert "2 patch" in message
        assert "minor" not in message
        assert "major" not in message

    def test_non_fork_counts_every_parent(
        self,
        tmp_path: Path,
        make_fork_history: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_fork_history(tmp_path)
        _drop_upstream_tag(repo)
        _declare(repo, "hyperi")
        monkeypatch.chdir(repo)
        _tag, releasable = unreleased_since_tag(repo)
        assert {bump for _sha, bump in releasable} >= {"minor", "major"}
