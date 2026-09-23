# Project:   HyperI CI
# File:      tests/unit/test_commit_range.py
# Purpose:   Tests for the pushed-range resolver and its release-worthiness
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.commit_range`.

The resolver half moved here with the code it tests (it was in
test_commit_validation.py). The `is_release_worthy` half is what the
predict-version gate asks before skipping quality + test on a merge to main
(issue #124). The `unreleased_warning` half is the cumulative question the
same gate asks on a validate-only run.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from hyperi_ci.commit_range import (
    commits_in_range,
    is_release_worthy,
    is_zero_sha,
    unreleased_since_tag,
    unreleased_warning,
)

_SECONDS_PER_DAY = 86400


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


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.io")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def _commit(cwd: Path, msg: str) -> str:
    _git(cwd, "commit", "--allow-empty", "-q", "-m", msg)
    return _git(cwd, "rev-parse", "HEAD")


def _commit_at(cwd: Path, msg: str, days_ago: int) -> str:
    """Commit dated ``days_ago``, so the age a warning quotes is deterministic."""
    stamp = f"{int(time.time()) - days_ago * _SECONDS_PER_DAY} +0000"
    env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", msg],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return _git(cwd, "rev-parse", "HEAD")


def _write_push_event(tmp_path: Path, before: str, after: str) -> Path:
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps({"before": before, "after": after}))
    return payload


def _on_push(monkeypatch: pytest.MonkeyPatch, repo: Path, payload: Path) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(payload))


class TestCommitsInRange:
    def test_push_range_uses_before_after(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        base = _commit(repo, "chore: seed")
        _commit(repo, "fix: one")
        head = _commit(repo, "feat: two")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, base, head))

        commits, resolved = commits_in_range()
        assert resolved is True
        subjects = [m.splitlines()[0] for _, m in commits]
        assert subjects == ["feat: two", "fix: one"]  # not the seed before `base`

    def test_push_empty_range_is_resolved_not_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # before == after (e.g. a re-run): a legit "no new commits", resolved.
        repo = _repo(tmp_path)
        head = _commit(repo, "fix: only")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, head, head))

        commits, resolved = commits_in_range()
        assert resolved is True
        assert commits == []

    def test_push_new_branch_zero_before_is_unresolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A branch-creation push has an all-zeros `before` -> range can't be
        # derived from it -> unresolved (caller must not treat as success).
        repo = _repo(tmp_path)
        head = _commit(repo, "fix: first")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, "0" * 40, head))

        _commits, resolved = commits_in_range()
        assert resolved is False

    def test_missing_before_commit_is_unresolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `before` names a SHA not in the (shallow) clone -> git errors -> the
        # push path can't resolve, and with no origin/main it stays unresolved.
        repo = _repo(tmp_path)
        _commit(repo, "fix: a")
        head = _commit(repo, "fix: b")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, "deadbeef" * 5, head))

        _commits, resolved = commits_in_range()
        assert resolved is False


class TestIsReleaseWorthy:
    """What the gate asks before skipping quality + test on a push to main."""

    def _worthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *subjects: str
    ) -> tuple[bool, str]:
        repo = _repo(tmp_path)
        base = _commit(repo, "chore: seed")
        for subject in subjects:
            _commit(repo, subject)
        head = _git(repo, "rev-parse", "HEAD")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, base, head))
        return is_release_worthy()

    @pytest.mark.parametrize(
        "subject",
        ["feat: add a source", "fix: an off-by-one", "perf: fewer allocations"],
    )
    def test_a_bumping_type_is_worthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subject: str
    ) -> None:
        worthy, _reason = self._worthy(tmp_path, monkeypatch, subject)
        assert worthy is True

    def test_a_breaking_marker_is_worthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worthy, reason = self._worthy(
            tmp_path, monkeypatch, "fix: rename\n\nBREAKING CHANGE: callers move"
        )
        assert worthy is True
        assert "major" in reason

    @pytest.mark.parametrize(
        "subject",
        ["chore: bump deps", "docs: fix a typo", "test: cover the parser"],
    )
    def test_a_non_bumping_type_is_not_worthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subject: str
    ) -> None:
        worthy, _reason = self._worthy(tmp_path, monkeypatch, subject)
        assert worthy is False

    def test_a_mixed_range_is_worthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The squash-merge shape: housekeeping either side of one real fix.
        worthy, reason = self._worthy(
            tmp_path,
            monkeypatch,
            "docs: note the flag",
            "fix: handle an empty payload",
            "chore: tidy imports",
        )
        assert worthy is True
        assert "patch" in reason

    def test_an_empty_resolved_range_is_not_worthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Resolved-and-empty is a real answer, not a degradation.
        repo = _repo(tmp_path)
        head = _commit(repo, "fix: only")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, head, head))

        worthy, _reason = is_release_worthy()
        assert worthy is False

    def test_an_unresolvable_range_runs_the_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE fail-safe: a shallow clone must open the gate, never close it.
        # Closing it is the silent skip design principle 3 forbids.
        repo = _repo(tmp_path)
        _commit(repo, "chore: nothing release-worthy here")
        head = _git(repo, "rev-parse", "HEAD")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, "deadbeef" * 5, head))

        worthy, reason = is_release_worthy()
        assert worthy is True
        assert "could not resolve" in reason

    def test_a_releaserc_override_decides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The bump map is the release_rules SSoT, not a second opinion: a repo
        # that promotes docs: gets its quality gate on a docs-only merge.
        repo = _repo(tmp_path)
        (repo / ".releaserc.json").write_text(
            json.dumps(
                {
                    "plugins": [
                        [
                            "@semantic-release/commit-analyzer",
                            {"releaseRules": [{"type": "docs", "release": "patch"}]},
                        ]
                    ]
                }
            ),
            encoding="utf-8",
        )
        _git(repo, "add", ".releaserc.json")
        base = _commit(repo, "chore: add releaserc")
        _commit(repo, "docs: rewrite the readme")
        head = _git(repo, "rev-parse", "HEAD")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, base, head))

        worthy, _reason = is_release_worthy()
        assert worthy is True


class TestUnreleasedWarning:
    """The scalo-rs shape: main green on every run, 26 days behind crates.io.

    A validate-only push publishes nothing by design and says so in a line
    that collapses into a folded log group. Nothing distinguished "nothing to
    ship" from "13 releasable commits waiting", one of them a security floor
    bump, for 26 days across six downstream consumers.
    """

    def _repo_at(self, tmp_path: Path, days_ago: int, *subjects: str) -> Path:
        """A repo tagged v1.2.3 ``days_ago``, with ``subjects`` landed since."""
        repo = _repo(tmp_path)
        _commit_at(repo, "chore: seed", days_ago)
        _git(repo, "tag", "v1.2.3")
        for subject in subjects:
            _commit(repo, subject)
        return repo

    def test_releasable_work_waiting_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The measured case, scaled down: fixes and a feature stacked behind
        # a tag, every run of which reported success.
        repo = self._repo_at(
            tmp_path,
            26,
            "fix(deps): raise floors off three live advisories",
            "fix: an off-by-one",
            "feat: a new source",
            "chore: tidy imports",
        )
        monkeypatch.chdir(repo)

        warn, message = unreleased_warning()
        assert warn is True
        assert "3 releasable commits sit unreleased since v1.2.3" in message
        assert "1 minor, 2 patch" in message
        assert "26 days ago" in message
        assert "hyperi-ci push --publish" in message

    def test_nothing_releasable_stays_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other half of the gate. A warning on a run with nothing waiting
        # is the noise that teaches people to stop reading warnings.
        repo = self._repo_at(
            tmp_path, 3, "chore: bump deps", "docs: fix a typo", "test: cover it"
        )
        monkeypatch.chdir(repo)

        warn, message = unreleased_warning()
        assert warn is False
        assert message == "nothing releasable waiting since v1.2.3"

    def test_head_on_the_tag_is_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_at(tmp_path, 0)
        monkeypatch.chdir(repo)

        warn, _message = unreleased_warning()
        assert warn is False

    def test_no_baseline_is_its_own_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The third outcome. A tag-less repo cannot be behind anything, and
        # folding that into either of the other two destroys the distinction
        # (docs/lessons.md, "A check has THREE outcomes").
        repo = _repo(tmp_path)
        _commit(repo, "fix: the very first commit")
        monkeypatch.chdir(repo)

        warn, message = unreleased_warning()
        assert warn is False
        assert "no released baseline" in message
        assert "nothing releasable" not in message

    def test_one_commit_reads_as_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_at(tmp_path, 1, "fix: a lone patch")
        monkeypatch.chdir(repo)

        _warn, message = unreleased_warning()
        assert "1 releasable commit sits unreleased" in message
        assert "tagged 1 day ago" in message

    def test_a_breaking_commit_counts_as_major(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_at(
            tmp_path, 2, "fix: rename the flag\n\nBREAKING CHANGE: callers move"
        )
        monkeypatch.chdir(repo)

        _warn, message = unreleased_warning()
        assert "1 major" in message

    def test_the_bump_map_is_the_release_rules_ssot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No second copy of the releasable types: a repo that promotes docs:
        # in its own .releaserc.json is counted as waiting on a docs commit.
        repo = _repo(tmp_path)
        (repo / ".releaserc.json").write_text(
            json.dumps(
                {
                    "plugins": [
                        [
                            "@semantic-release/commit-analyzer",
                            {"releaseRules": [{"type": "docs", "release": "patch"}]},
                        ]
                    ]
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        _git(repo, "add", ".releaserc.json")
        _commit(repo, "chore: add releaserc")
        _git(repo, "tag", "v0.1.0")
        _commit(repo, "docs: rewrite the readme")
        monkeypatch.chdir(repo)

        warn, message = unreleased_warning()
        assert warn is True
        assert "1 patch" in message

    def test_a_non_version_tag_is_not_a_baseline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Releases are v* tags; a `nightly` or `latest` marker is not one, and
        # measuring from it would report the backlog as already shipped.
        repo = _repo(tmp_path)
        _commit(repo, "fix: shipped long ago")
        _git(repo, "tag", "v1.0.0")
        _commit(repo, "fix: waiting")
        _git(repo, "tag", "nightly")
        monkeypatch.chdir(repo)

        tag, releasable = unreleased_since_tag()
        assert tag == "v1.0.0"
        assert len(releasable) == 1


def test_is_zero_sha() -> None:
    assert is_zero_sha("0" * 40) is True
    assert is_zero_sha("0" * 7) is True
    assert is_zero_sha("deadbeef") is False
    assert is_zero_sha("000000") is False  # too short to be the sentinel
