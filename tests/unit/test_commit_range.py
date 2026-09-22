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
(issue #124).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hyperi_ci.commit_range import commits_in_range, is_release_worthy, is_zero_sha


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.io")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def _commit(cwd: Path, msg: str) -> str:
    _git(cwd, "commit", "--allow-empty", "-q", "-m", msg)
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


def test_is_zero_sha() -> None:
    assert is_zero_sha("0" * 40) is True
    assert is_zero_sha("0" * 7) is True
    assert is_zero_sha("deadbeef") is False
    assert is_zero_sha("000000") is False  # too short to be the sentinel
