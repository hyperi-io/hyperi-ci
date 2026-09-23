# Project:   HyperI CI
# File:      tests/unit/test_rehearse_branch.py
# Purpose:   Tests for the branch-rehearsal ref swap
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/rehearse-branch.py pure helpers."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "rehearse_branch",
    Path(__file__).resolve().parents[2] / "scripts" / "rehearse-branch.py",
)
assert _SPEC is not None and _SPEC.loader is not None
rehearse_branch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rehearse_branch)


class TestSwapRefs:
    def test_swaps_workflow_ref(self) -> None:
        text = "    uses: hyperi-io/hyperi-ci/.github/workflows/go-ci.yml@main\n"
        out, count = rehearse_branch.swap_refs(text, "fix/my-change")
        assert count == 1
        assert "hyperi-io/hyperi-ci/.github/workflows/go-ci.yml@fix/my-change" in out
        assert "@main" not in out

    def test_swaps_multiple_refs(self) -> None:
        text = (
            "uses: hyperi-io/hyperi-ci/.github/workflows/rust-ci.yml@main\n"
            "uses: hyperi-io/hyperi-ci/.github/workflows/_ghcr-prune.yml@main\n"
        )
        out, count = rehearse_branch.swap_refs(text, "fix/x")
        assert count == 2
        assert out.count("@fix/x") == 2

    def test_leaves_third_party_refs_alone(self) -> None:
        text = "uses: actions/checkout@main\nuses: dataaxiom/ghcr-cleanup-action@main\n"
        out, count = rehearse_branch.swap_refs(text, "fix/x")
        assert count == 0
        assert out == text

    def test_leaves_pinned_refs_alone(self) -> None:
        # A SHA-pinned or version-pinned hyperi-ci ref is deliberate -- only
        # the floating @main refs are rehearsal targets.
        text = "uses: hyperi-io/hyperi-ci/.github/workflows/go-ci.yml@abc123\n"
        out, count = rehearse_branch.swap_refs(text, "fix/x")
        assert count == 0
        assert out == text

    def test_does_not_match_main_prefix_words(self) -> None:
        # @maintenance must not be treated as @main (word boundary).
        text = "uses: hyperi-io/hyperi-ci/.github/workflows/go-ci.yml@maintenance\n"
        out, count = rehearse_branch.swap_refs(text, "fix/x")
        assert count == 0
        assert out == text


class TestRehearseSlug:
    def test_slashes_collapse(self) -> None:
        assert rehearse_branch.rehearse_slug("fix/branch-rehearsal") == (
            "fix-branch-rehearsal"
        )

    def test_weird_chars_collapse_and_trim(self) -> None:
        assert rehearse_branch.rehearse_slug("feat/x y!(z)..") == "feat-x-y-z"

    def test_length_capped(self) -> None:
        assert len(rehearse_branch.rehearse_slug("x" * 200)) == 80


class TestSummariseJobs:
    """The rehearsal verdict is read job by job, never from the run status."""

    # The shape of a real fixture PR run: the tag job skips on a PR.
    PR_RUN = [
        {"name": "ci / Quality", "conclusion": "success"},
        {"name": "ci / Build (linux-amd64)", "conclusion": "success"},
        {"name": "ci / Release tail / Tag & Release", "conclusion": "skipped"},
    ]

    def test_a_green_pr_run_passes(self) -> None:
        passed, lines = rehearse_branch.summarise_jobs(self.PR_RUN)
        assert passed is True
        assert len(lines) == 3

    def test_one_failed_job_fails_the_rehearsal(self) -> None:
        jobs = [*self.PR_RUN, {"name": "ci / Test", "conclusion": "failure"}]
        passed, lines = rehearse_branch.summarise_jobs(jobs)
        assert passed is False
        assert any("failure" in line and "ci / Test" in line for line in lines)

    def test_a_run_with_no_jobs_proves_nothing(self) -> None:
        assert rehearse_branch.summarise_jobs([]) == (False, [])

    def test_an_unfinished_job_is_not_a_pass(self) -> None:
        passed, _ = rehearse_branch.summarise_jobs(
            [{"name": "ci / Build", "conclusion": ""}]
        )
        assert passed is False


class TestMergeRefRace:
    """A rehearsal must not report a GitHub timing artefact as a broken branch.

    GitHub computes ``refs/pull/N/merge`` asynchronously but fires the
    pull_request workflow on PR creation, so checkout can be told to fetch a
    ref that does not exist yet. It retries three times over ~20s and fails
    the job, which reads as the rehearsed branch being broken.
    """

    @staticmethod
    def _completed(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def test_it_waits_until_the_merge_ref_resolves(self, monkeypatch) -> None:
        attempts = []

        def fake_run(args, **_kwargs):
            attempts.append(args)
            rc = 0 if len(attempts) >= 3 else 1
            return subprocess.CompletedProcess(args, rc, stdout="", stderr="")

        monkeypatch.setattr(rehearse_branch, "_run", fake_run)
        monkeypatch.setattr(rehearse_branch.time, "sleep", lambda _s: None)
        assert rehearse_branch._wait_for_merge_ref("o/r", 19) is True
        assert len(attempts) == 3
        assert "repos/o/r/git/ref/pull/19/merge" in attempts[0]

    def test_a_ref_that_never_appears_is_reported(self, monkeypatch) -> None:
        monkeypatch.setattr(
            rehearse_branch,
            "_run",
            lambda args, **_k: subprocess.CompletedProcess(args, 1),
        )
        monkeypatch.setattr(rehearse_branch.time, "sleep", lambda _s: None)
        assert rehearse_branch._wait_for_merge_ref("o/r", 19, timeout_secs=0) is False

    def test_the_rerun_waits_for_the_run_to_leave_completed(self, monkeypatch) -> None:
        """Without this the caller re-reads the stale conclusion it is retrying."""
        statuses = iter(['{"status":"completed"}', '{"status":"in_progress"}'])
        polled: list[list[str]] = []

        def fake_run(args, **_kwargs):
            if "rerun" in args:
                return subprocess.CompletedProcess(args, 0)
            polled.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=next(statuses))

        monkeypatch.setattr(rehearse_branch, "_run", fake_run)
        monkeypatch.setattr(rehearse_branch.time, "sleep", lambda _s: None)
        assert rehearse_branch._rerun("o/r", 42) is True
        # Returning on the first read would hand back the conclusion being retried.
        assert len(polled) == 2

    def test_a_refused_rerun_is_not_a_restart(self, monkeypatch) -> None:
        monkeypatch.setattr(
            rehearse_branch,
            "_run",
            lambda args, **_k: subprocess.CompletedProcess(args, 1),
        )
        assert rehearse_branch._rerun("o/r", 42) is False


class TestPickRun:
    """Selecting on the fixture commit, because the branch name is reused.

    A stale GREEN run read as this cycle's result certifies a hyperi-ci commit
    no fixture ever ran, which is the gate not existing (issue #263).
    """

    MINE = {"databaseId": 2, "status": "completed", "headSha": "bd5cb02e"}
    STALE = {"databaseId": 1, "status": "completed", "headSha": "579ee253"}

    def test_a_previous_cycles_run_is_not_mine(self) -> None:
        assert rehearse_branch.pick_run([self.STALE], "bd5cb02e") is None

    def test_it_reaches_past_a_stale_head_of_list_entry(self) -> None:
        # gh returns newest-first, but indexing lag can put the stale one there.
        picked = rehearse_branch.pick_run([self.STALE, self.MINE], "bd5cb02e")
        assert picked is not None
        assert picked["databaseId"] == 2

    def test_a_stale_green_run_never_stands_in_for_mine(self) -> None:
        green_stale = {**self.STALE, "conclusion": "success"}
        assert rehearse_branch.pick_run([green_stale], "bd5cb02e") is None

    def test_no_runs_yet_is_not_a_match(self) -> None:
        assert rehearse_branch.pick_run([], "bd5cb02e") is None
