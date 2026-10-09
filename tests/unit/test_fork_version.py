# Project:   HyperI CI
# File:      tests/unit/test_fork_version.py
# Purpose:   Tests for first-parent versioning of a fork's release
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.fork_version`.

Every history here is a real git repository. The fork merges an upstream that
added a ``feat:`` and a breaking ``feat!:``.
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from hyperi_ci.commit_range import git_log, last_version_tag
from hyperi_ci.fork_version import (
    ForkVersionError,
    check_fork,
    next_version,
    predict_version,
)
from hyperi_ci.release_rules import classify_commit

#: The tag make_fork_history puts on upstream, reachable only through the merge.
UPSTREAM_TAG = "v9.0.0"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()


class TestASyncMerge:
    """Upstream's commits arrive through the merge and decide nothing."""

    def test_the_full_walk_sees_upstreams_breaking_change(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        # The control: what semantic-release's walk of every commit counts.
        repo = make_fork_history(tmp_path)
        _, commits = git_log(["v0.2.8..HEAD"], cwd=repo)
        bumps = {classify_commit(message) for _, message in commits}
        assert "major" in bumps
        assert last_version_tag(cwd=repo) == UPSTREAM_TAG

    def test_own_fix_gives_a_patch(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        version, how = predict_version(repo)
        assert version == "0.2.9"
        assert how.startswith("patch from v0.2.8 over 3 first-parent commit(s)")

    def test_the_upstream_tag_is_not_the_last_release(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        assert last_version_tag(first_parent=True, cwd=repo) == "v0.2.8"

    def test_own_feat_gives_a_minor(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path, own=("fix: a", "feat: own feature"))
        assert predict_version(repo)[0] == "0.3.0"

    def test_own_breaking_change_gives_a_major(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path, own=("fix!: own break",))
        assert predict_version(repo)[0] == "1.0.0"

    def test_a_squashed_sync_is_classified_by_its_own_title(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path, own=("chore: sync",))
        _git(repo, "commit", "--allow-empty", "-q", "-m", "feat: sync upstream 2.1")
        assert predict_version(repo)[0] == "0.3.0"

    def test_a_sync_merge_alone_ships_as_a_patch(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        """A GitHub merge subject is not a conventional commit, and the sync must ship."""
        repo = make_fork_history(tmp_path, own=())
        version, how = predict_version(repo)
        assert version == "0.2.9"
        assert "Merge pull request #121" in how

    def test_a_merge_body_quoting_a_breaking_change_is_still_a_patch(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path, own=())
        _git(repo, "reset", "-q", "--hard", "HEAD~2")
        _git(
            repo,
            "merge",
            "--no-ff",
            "-q",
            "upstream",
            "-m",
            "Merge pull request #122 from hyperi-io/sync\n\nBREAKING CHANGE: upstream",
        )
        assert predict_version(repo)[0] == "0.2.9"

    def test_a_merge_with_a_conventional_subject_keeps_its_bump(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path, own=())
        _git(repo, "reset", "-q", "--hard", "HEAD~2")
        _git(repo, "merge", "--no-ff", "-q", "upstream", "-m", "feat: sync upstream")
        assert predict_version(repo)[0] == "0.3.0"

    def test_nothing_of_its_own_releases_nothing(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        _git(repo, "tag", "v0.2.9")
        _git(repo, "commit", "--allow-empty", "-q", "-m", "docs: more docs")
        with pytest.raises(ForkVersionError, match="No release-worthy first-parent"):
            predict_version(repo)

    def test_a_releaserc_override_is_honoured(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path, own=("feat: own feature",))
        rules = [{"type": "feat", "release": "patch"}]
        (repo / ".releaserc.json").write_text(
            json.dumps(
                {
                    "plugins": [
                        ["@semantic-release/commit-analyzer", {"releaseRules": rules}]
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert predict_version(repo)[0] == "0.2.9"

    def test_a_prerelease_tag_is_not_the_base(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        _git(repo, "tag", "v0.3.0-beta.1", "HEAD~1")
        assert predict_version(repo)[0] == "0.2.9"


class TestTheGuards:
    def test_a_taken_tag_off_head_is_refused(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        _git(repo, "tag", "v0.2.9", "upstream")
        with pytest.raises(ForkVersionError, match="v0.2.9 already exists"):
            predict_version(repo)

    def test_only_an_upstream_tag_is_refused(self, tmp_path: Path) -> None:
        # The fork has never released, but upstream's tag came in by the merge.
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "ci@example.invalid")
        _git(tmp_path, "config", "user.name", "CI")
        _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "chore: root")
        _git(tmp_path, "checkout", "-q", "-b", "upstream")
        _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "feat: upstream")
        _git(tmp_path, "tag", "v1.0.0")
        _git(tmp_path, "checkout", "-q", "main")
        _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "fix: own")
        _git(tmp_path, "merge", "--no-ff", "-q", "upstream", "-m", "Merge upstream")
        with pytest.raises(ForkVersionError, match="none is on HEAD's first-parent"):
            predict_version(tmp_path)

    def test_a_tag_less_repo_starts_at_its_seed(self, tmp_path: Path) -> None:
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "ci@example.invalid")
        _git(tmp_path, "config", "user.name", "CI")
        _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "fix: own")
        (tmp_path / "package.json").write_text('{"version": "2.4.0"}', encoding="utf-8")
        version, how = predict_version(tmp_path)
        assert version == "2.4.0"
        assert "package.json" in how

    def test_a_tag_less_repo_with_nothing_to_release_fails(
        self, tmp_path: Path
    ) -> None:
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "ci@example.invalid")
        _git(tmp_path, "config", "user.name", "CI")
        _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "docs: readme")
        with pytest.raises(ForkVersionError, match="tag-less repo"):
            predict_version(tmp_path)

    def test_a_non_repo_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ForkVersionError, match="git log --first-parent HEAD"):
            predict_version(tmp_path)


class TestCheckFork:
    @pytest.fixture(autouse=True)
    def no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_CLASSIFICATION", raising=False)

    @pytest.mark.parametrize("value", ["fork", "Fork", "3"])
    def test_the_config_declares_it(self, tmp_path: Path, value: str) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            f"classification: {value}\n", encoding="utf-8"
        )
        check = check_fork(tmp_path)
        assert check.fork
        assert check.warning == ""

    def test_the_dotfile_declares_it(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-classification").write_text("fork\n", encoding="utf-8")
        assert check_fork(tmp_path).fork

    @pytest.mark.parametrize("value", ["internal", "general-oss", "product"])
    def test_another_category_is_not_a_fork(self, tmp_path: Path, value: str) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            f"classification: {value}\n", encoding="utf-8"
        )
        check = check_fork(tmp_path)
        assert not check.fork
        assert check.warning == ""

    def test_undeclared_is_not_a_fork_and_is_quiet(self, tmp_path: Path) -> None:
        check = check_fork(tmp_path)
        assert check == (False, "classification undeclared (undeclared)", "")

    def test_a_typo_reads_as_undeclared_and_warns(self, tmp_path: Path) -> None:
        # `hyperi-ci config` reads it the same way, so the tail agrees.
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "classification: frok\n", encoding="utf-8"
        )
        check = check_fork(tmp_path)
        assert not check.fork
        assert "Unknown classification 'frok'" in check.warning

    def test_the_env_override_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "classification: internal\n", encoding="utf-8"
        )
        monkeypatch.setenv("HYPERCI_CLASSIFICATION", "fork")
        assert check_fork(tmp_path).fork

    def test_an_unreadable_config_still_reads_the_dotfile(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("x: [\n", encoding="utf-8")
        (tmp_path / ".hyperi-classification").write_text("fork\n", encoding="utf-8")
        assert check_fork(tmp_path) == (
            True,
            "classification fork (.hyperi-classification)",
            "",
        )

    def test_an_unreadable_config_without_a_dotfile_warns(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("x: [\n", encoding="utf-8")
        check = check_fork(tmp_path)
        assert not check.fork
        assert check.warning.startswith(".hyperi-ci.yaml could not be read")


class TestNextVersion:
    @pytest.mark.parametrize(
        ("base", "bump", "expected"),
        [
            ("0.2.8", "patch", "0.2.9"),
            ("0.2.8", "minor", "0.3.0"),
            ("0.2.8", "major", "1.0.0"),
            ("1.9.9", "patch", "1.9.10"),
            ("0.0.0", "patch", "0.0.1"),
        ],
    )
    def test_levels(self, base: str, bump: str, expected: str) -> None:
        assert next_version(base, bump) == expected

    @pytest.mark.parametrize(("base", "bump"), [("0.2.8", "none"), ("0.2", "patch")])
    def test_bad_input_raises(self, base: str, bump: str) -> None:
        with pytest.raises(ValueError):
            next_version(base, bump)
