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
(issue #124). The `unreleased_annotation` half is the cumulative question the
same gate asks on a validate-only run.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from hyperi_ci.commit_range import (
    commits_in_range,
    is_harmless_path,
    is_release_worthy,
    is_zero_sha,
    touches_code,
    unreleased_annotation,
    unreleased_since_tag,
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


def _upstream_sync(tmp_path: Path) -> tuple[Path, str, str]:
    """A main that merges an upstream branch of non-conventional commits."""
    repo = _repo(tmp_path)
    base = _commit(repo, "chore: seed")
    main = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-q", "-b", "upstream")
    _commit(repo, "Add a dashboard")
    _commit(repo, "Bump deps (#123)")
    _git(repo, "checkout", "-q", main)
    _commit(repo, "fix: our own change")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge upstream into main", "upstream")
    return repo, base, _git(repo, "rev-parse", "HEAD")


class TestFirstParent:
    """A fork validates and gates on its own commits, never on upstream's."""

    def test_the_full_range_includes_upstream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, base, head = _upstream_sync(tmp_path)
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, base, head))
        subjects = {m.splitlines()[0] for _, m in commits_in_range()[0]}
        assert "Add a dashboard" in subjects

    def test_first_parent_leaves_upstream_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, base, head = _upstream_sync(tmp_path)
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, base, head))
        commits, resolved = commits_in_range(first_parent=True)
        assert resolved is True
        subjects = [m.splitlines()[0] for _, m in commits]
        assert subjects == ["Merge upstream into main", "fix: our own change"]

    def test_a_declared_fork_gates_on_its_own_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, base, head = _upstream_sync(tmp_path)
        (repo / ".hyperi-ci.yaml").write_text(
            "classification: fork\n", encoding="utf-8"
        )
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, base, head))
        worthy, reason = is_release_worthy()
        assert worthy is True
        assert "fix: our own change" in reason


def _on_merge_group(
    monkeypatch: pytest.MonkeyPatch, repo: Path, tmp_path: Path, group: dict
) -> None:
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps({"merge_group": group}))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(payload))


class TestMergeGroupRange:
    """A merge queue entry's range comes from its payload (issue #228)."""

    def test_range_is_base_sha_to_head_sha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        _commit(repo, "chore: already on main")
        base = _commit(repo, "fix: the base the queue built on")
        head = _commit(repo, "fix: the squash that lands (#7)")
        _commit(repo, "feat: past head, not in the group")
        _on_merge_group(
            monkeypatch, repo, tmp_path, {"base_sha": base, "head_sha": head}
        )

        commits, resolved = commits_in_range()
        assert resolved is True
        assert [m.splitlines()[0] for _, m in commits] == [
            "fix: the squash that lands (#7)"
        ]

    def test_unknown_base_is_unresolved_not_a_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A base the clone lacks must degrade loudly, never fall through to a
        # generic range that validates some other set of commits.
        repo = _repo(tmp_path)
        head = _commit(repo, "fix: head")
        _on_merge_group(
            monkeypatch, repo, tmp_path, {"base_sha": "deadbeef" * 5, "head_sha": head}
        )

        commits, resolved = commits_in_range()
        assert (commits, resolved) == ([], False)


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

    def test_a_repo_releaserc_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CI tags with the central config, so a repo promoting docs: must not
        # make the gate expect a release the tag step will not cut.
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
        assert worthy is False


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

        level, message = unreleased_annotation(warn_days=0)
        assert level == "warning"
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

        level, message = unreleased_annotation(warn_days=0)
        assert level == ""
        assert message == "nothing releasable waiting since v1.2.3"

    def test_head_on_the_tag_is_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_at(tmp_path, 0)
        monkeypatch.chdir(repo)

        level, _message = unreleased_annotation(warn_days=0)
        assert level == ""

    def test_no_baseline_is_its_own_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The third outcome. A tag-less repo cannot be behind anything, and
        # folding that into either of the other two destroys the distinction
        # (docs/lessons.md, "A check has THREE outcomes").
        repo = _repo(tmp_path)
        _commit(repo, "fix: the very first commit")
        monkeypatch.chdir(repo)

        level, message = unreleased_annotation(warn_days=0)
        assert level == ""
        assert "no released baseline" in message
        assert "nothing releasable" not in message

    def test_one_commit_reads_as_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_at(tmp_path, 1, "fix: a lone patch")
        monkeypatch.chdir(repo)

        _level, message = unreleased_annotation(warn_days=0)
        assert "1 releasable commit sits unreleased" in message
        assert "tagged 1 day ago" in message

    def test_a_breaking_commit_counts_as_major(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_at(
            tmp_path, 2, "fix: rename the flag\n\nBREAKING CHANGE: callers move"
        )
        monkeypatch.chdir(repo)

        _level, message = unreleased_annotation(warn_days=0)
        assert "1 major" in message

    def test_a_repo_releaserc_does_not_count_a_docs_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The central config decides what is releasable, so a docs: commit is
        # not waiting on a release whatever the repo's own config says.
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

        level, message = unreleased_annotation(warn_days=0)
        assert level == ""
        assert message == "nothing releasable waiting since v0.1.0"

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


class TestUnreleasedAge:
    """Unreleased work is a notice while young, a warning once it has waited."""

    # What the gate printed before the age gate existed. A consumer keys its
    # findings on this text, so the warning must still read exactly so.
    PINNED = (
        "2 releasable commits sit unreleased since v1.2.3, tagged 26 days ago "
        "(1 minor, 1 patch). This run validated them and published nothing. "
        "Ship them with 'hyperi-ci push --publish', or re-run this workflow "
        "with from-head=true."
    )

    def _repo(self, tmp_path: Path, *ages: int) -> Path:
        """A repo tagged v1.2.3 26 days ago, then one fix: per age in days."""
        repo = _repo(tmp_path)
        _commit_at(repo, "chore: seed", 26)
        _git(repo, "tag", "v1.2.3")
        for index, age in enumerate(ages):
            _commit_at(repo, f"fix: waiting {index}", age)
        return repo

    def test_old_work_warns_with_the_pinned_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        _commit_at(repo, "chore: seed", 26)
        _git(repo, "tag", "v1.2.3")
        _commit_at(repo, "fix: an off-by-one", 20)
        _commit_at(repo, "feat: a new source", 20)
        monkeypatch.chdir(repo)

        assert unreleased_annotation(warn_days=7) == ("warning", self.PINNED)

    def test_young_work_is_a_notice_with_the_same_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(self._repo(tmp_path, 0, 0))

        level, message = unreleased_annotation(warn_days=7)
        assert level == "notice"
        assert message == unreleased_annotation(warn_days=0)[1]

    @pytest.mark.parametrize(("warn_days", "level"), [(14, "notice"), (7, "warning")])
    def test_the_threshold_decides(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        warn_days: int,
        level: str,
    ) -> None:
        monkeypatch.chdir(self._repo(tmp_path, 10))

        assert unreleased_annotation(warn_days=warn_days)[0] == level

    def test_the_oldest_waiting_commit_decides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A fresh fix on top must not reset the clock on a ten-day-old one.
        monkeypatch.chdir(self._repo(tmp_path, 10, 0))

        assert unreleased_annotation(warn_days=7)[0] == "warning"

    def test_no_threshold_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(self._repo(tmp_path, 0))

        assert unreleased_annotation(warn_days=None)[0] == "warning"


def _commit_files(repo: Path, msg: str, files: dict[str, str]) -> str:
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="\n")
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


class TestTouchesCode:
    """issue #96: a merge to main skipped its checks on commit type alone.

    A `chore:` or `test:` that edits code is not release-worthy, so the gate
    let it land unchecked. The files the range changed decide as well.
    """

    def _push(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        msg: str,
        files: dict[str, str],
    ) -> tuple[bool, str]:
        repo = _repo(tmp_path)
        base = _commit_files(repo, "chore: seed", {"seed.txt": "seed\n"})
        head = _commit_files(repo, msg, files)
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, base, head))
        return touches_code()

    def test_docs_and_metadata_only_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        touched, reason = self._push(
            tmp_path,
            monkeypatch,
            "docs: the guide",
            {
                "README.md": "x\n",
                "docs/guide.txt": "x\n",
                "img/logo.svg": "<svg/>\n",
                ".gitignore": "x\n",
                "CHANGELOG.md": "x\n",
                "LICENSE": "x\n",
                "VERSION": "1.0.0\n",
            },
        )
        assert touched is False
        assert reason.startswith("only docs and repo metadata changed: ")
        assert "README.md" in reason
        assert "and 2 more" in reason

    @pytest.mark.parametrize(
        "path",
        ["src/app.py", "src/lib.rs", ".github/workflows/ci.yml", "pyproject.toml"],
    )
    def test_a_chore_that_edits_code_runs_the_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
    ) -> None:
        touched, reason = self._push(
            tmp_path,
            monkeypatch,
            "chore: tidy",
            {path: "x\n", "README.md": "x\n"},
        )
        assert touched is True
        assert path in reason
        assert "README.md" not in reason

    def test_moving_code_into_docs_runs_the_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rename detection would list only the new docs/ path.
        repo = _repo(tmp_path)
        base = _commit_files(repo, "chore: seed", {"src/app.py": "x = 1\n"})
        (repo / "docs").mkdir()
        _git(repo, "mv", "src/app.py", "docs/app.md")
        _git(repo, "commit", "-q", "-m", "docs: keep it as an example")
        head = _git(repo, "rev-parse", "HEAD")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, base, head))

        touched, reason = touches_code()
        assert touched is True
        assert "src/app.py" in reason

    def test_an_unresolvable_range_runs_the_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        head = _commit_files(repo, "docs: only", {"README.md": "x\n"})
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, "deadbeef" * 5, head))

        touched, reason = touches_code()
        assert touched is True
        assert "could not resolve" in reason

    def test_a_rewind_runs_the_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A force-push back to an older tip adds no commits, and says nothing
        # about what main now holds.
        repo = _repo(tmp_path)
        older = _commit_files(repo, "chore: seed", {"app.py": "x = 1\n"})
        newer = _commit_files(repo, "docs: later", {"README.md": "x\n"})
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, newer, older))

        touched, reason = touches_code()
        assert touched is True
        assert "adds no commits" in reason

    def test_a_re_run_of_the_same_tip_runs_the_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        head = _commit(repo, "fix: only")
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, head, head))

        assert touches_code()[0] is True

    def test_creating_the_branch_runs_the_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An all-zeros `before` falls through to origin/main..HEAD, which says
        # nothing about the push.
        repo = _repo(tmp_path)
        head = _commit_files(repo, "docs: only", {"README.md": "x\n"})
        _on_push(monkeypatch, repo, _write_push_event(tmp_path, "0" * 40, head))

        touched, reason = touches_code()
        assert touched is True
        assert "created the branch" in reason

    @pytest.mark.parametrize(
        "path", ["src/LICENSE.py", "LICENSE_check.py", "CHANGELOG_gen.py"]
    )
    def test_code_named_like_metadata_runs_the_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
    ) -> None:
        touched, reason = self._push(
            tmp_path, monkeypatch, "chore: tidy", {path: "x\n"}
        )
        assert touched is True
        assert path in reason


@pytest.mark.parametrize(
    ("path", "harmless"),
    [
        ("docs/conf.txt", True),
        ("guide.RST", True),
        ("sub/.editorconfig", True),
        ("LICENSE", True),
        ("LICENSE.txt", True),
        ("LICENCE-APACHE", True),
        ("COPYING", True),
        ("CHANGELOG", True),
        ("CHANGELOG.rst", True),
        ("shot.JPEG", True),
        ("LICENSE-APACHE", False),
        ("src/LICENSE.py", False),
        ("LICENSE_check.py", False),
        ("CHANGELOG_gen.py", False),
        ("LICENCE.py", False),
        ("docs/conf.py", False),
        ("src/docs/thing.py", False),
        ("Makefile", False),
        ("tests/test_x.py", False),
        ("VERSION.py", False),
    ],
)
def test_is_harmless_path(path: str, harmless: bool) -> None:
    assert is_harmless_path(path) is harmless


def test_is_zero_sha() -> None:
    assert is_zero_sha("0" * 40) is True
    assert is_zero_sha("0" * 7) is True
    assert is_zero_sha("deadbeef") is False
    assert is_zero_sha("000000") is False  # too short to be the sentinel
