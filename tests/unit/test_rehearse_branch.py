# Project:   HyperI CI
# File:      tests/unit/test_rehearse_branch.py
# Purpose:   Tests for the branch-rehearsal ref swap
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/rehearse-branch.py pure helpers."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

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

    def test_the_watcher_reads_the_run_for_this_fixture_commit(
        self, monkeypatch
    ) -> None:
        """A newer run on the reused branch name must not stand in (issue #263).

        pick_run alone is tested elsewhere; this holds the watcher to using it.
        """
        listed = json.dumps(
            [
                {"databaseId": 99, "status": "completed", "headSha": "stale"},
                {"databaseId": 42, "status": "completed", "headSha": "mine"},
            ]
        )
        jobs = json.dumps({"jobs": [{"name": "ci / Test", "conclusion": "success"}]})
        viewed: list[str] = []

        def fake_run(args, **_kwargs):
            if args[:3] == ["gh", "run", "list"]:
                return subprocess.CompletedProcess(args, 0, stdout=listed)
            viewed.append(args[3])
            return subprocess.CompletedProcess(args, 0, stdout=jobs)

        monkeypatch.setattr(rehearse_branch, "_run", fake_run)
        verdict, _lines, run_id = rehearse_branch._watch_pr_run(
            "o/r", "rehearse/x", "mine", 1
        )
        assert (verdict, run_id, viewed) == ("pass", 42, ["42"])

    def test_a_refused_rerun_is_not_a_restart(self, monkeypatch) -> None:
        monkeypatch.setattr(
            rehearse_branch,
            "_run",
            lambda args, **_k: subprocess.CompletedProcess(args, 1),
        )
        assert rehearse_branch._rerun("o/r", 42) is False


class TestTeardownLeavesNothingRed:
    """What the rehearsal leaves on the fixture after it gives up or passes."""

    def test_unfinished_runs_are_cancelled_before_teardown(self, monkeypatch) -> None:
        """A queued run outliving its PR dies red at checkout (issue #260)."""
        listed = json.dumps(
            [
                {"databaseId": 1, "status": "completed"},
                {"databaseId": 2, "status": "queued"},
                {"databaseId": 3, "status": "in_progress"},
            ]
        )
        cancelled: list[str] = []

        def fake_run(args, **_kwargs):
            if "cancel" in args:
                cancelled.append(args[3])
                return subprocess.CompletedProcess(args, 0)
            return subprocess.CompletedProcess(args, 0, stdout=listed)

        monkeypatch.setattr(rehearse_branch, "_run", fake_run)
        assert rehearse_branch._cancel_inflight("o/r", "rehearse/x") == [2, 3]
        assert cancelled == ["2", "3"]

    def test_an_unset_override_reads_as_none(self, monkeypatch) -> None:
        monkeypatch.setattr(
            rehearse_branch,
            "_run",
            lambda a, **_k: subprocess.CompletedProcess(
                a, 1, stdout="", stderr="gh: Not Found (HTTP 404)"
            ),
        )
        assert rehearse_branch._read_override("o/r") is None

    def test_an_unreadable_override_is_not_read_as_unset(self, monkeypatch) -> None:
        """Read as unset, cleanup would DELETE a permanent override."""
        monkeypatch.setattr(
            rehearse_branch,
            "_run",
            lambda a, **_k: subprocess.CompletedProcess(
                a, 1, stdout="", stderr="gh: Bad credentials (HTTP 401)"
            ),
        )
        try:
            rehearse_branch._read_override("o/r")
        except rehearse_branch.OverrideUnreadableError as exc:
            assert "HTTP 401" in str(exc)
        else:
            raise AssertionError("an unreadable override was read as unset")


def _job(conclusion: str, *steps: tuple[str, str]) -> dict:
    return {
        "conclusion": conclusion,
        "steps": [{"name": name, "conclusion": c} for name, c in steps],
    }


class TestOnlyTheCheckoutRaceIsRerun:
    """A rerun hides a flaky red unless the failure was the merge-ref race."""

    def test_a_checkout_failure_is_the_race(self) -> None:
        jobs = [
            _job(
                "failure",
                ("Set up job", "success"),
                ("Run actions/checkout@abc", "failure"),
            ),
            _job("success", ("Run actions/checkout@abc", "success")),
        ]
        assert rehearse_branch.raced_the_merge_ref(jobs) is True

    def test_a_failing_test_step_is_the_branch(self) -> None:
        jobs = [
            _job(
                "failure",
                ("Run actions/checkout@abc", "success"),
                ("Run tests", "failure"),
            )
        ]
        assert rehearse_branch.raced_the_merge_ref(jobs) is False

    def test_one_real_failure_beside_a_checkout_failure_is_the_branch(self) -> None:
        jobs = [
            _job("failure", ("Run actions/checkout@abc", "failure")),
            _job(
                "failure",
                ("Run actions/checkout@abc", "success"),
                ("Run quality", "failure"),
            ),
        ]
        assert rehearse_branch.raced_the_merge_ref(jobs) is False

    def test_a_failed_job_with_no_failed_step_is_not_the_race(self) -> None:
        assert rehearse_branch.raced_the_merge_ref([_job("failure")]) is False

    def test_no_failure_is_not_the_race(self) -> None:
        assert rehearse_branch.raced_the_merge_ref([_job("success")]) is False


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


# ci-test-manifests pins @main on purpose, with no --python.
_MAIN_PIN = (
    "uvx --no-cache --refresh --from "
    "git+https://github.com/hyperi-io/hyperi-ci@main hyperi-ci"
)


class TestWhoHoldsAFixture:
    """A fixture is held while its install override runs a hyperi-ci branch."""

    def test_a_rehearsal_override_holds_it(self) -> None:
        value = rehearse_branch.override_value("fix/312-fetch-followups")
        assert rehearse_branch.held_by(value) == "fix/312-fetch-followups"

    def test_the_permanent_main_pin_does_not(self) -> None:
        assert rehearse_branch.held_by(_MAIN_PIN) is None

    @pytest.mark.parametrize("value", [None, ""])
    def test_an_unset_override_does_not(self, value) -> None:
        assert rehearse_branch.held_by(value) is None

    def test_a_branch_named_after_main_is_still_a_branch(self) -> None:
        value = rehearse_branch.override_value("main-next")
        assert rehearse_branch.held_by(value) == "main-next"

    def test_the_ref_runs_to_the_next_space(self) -> None:
        value = (
            "uvx --from git+https://github.com/hyperi-io/hyperi-ci@feat/a/b.c hyperi-ci"
        )
        assert rehearse_branch.held_by(value) == "feat/a/b.c"

    def test_a_ref_ending_the_value_is_read(self) -> None:
        value = "uvx --from git+https://github.com/hyperi-io/hyperi-ci.git@fix/x"
        assert rehearse_branch.held_by(value) == "fix/x"

    @pytest.mark.parametrize(
        "value",
        [
            # A PyPI version pin, not a git ref.
            "uvx --from hyperi-ci@2.10.12 hyperi-ci",
            "uvx --from git+https://github.com/hyperi-io/hyperi-ci-fork@fix/x hyperi-ci",
            "uvx --from git+https://github.com/someone/hyperi-ci@fix/x hyperi-ci",
            "pipx run hyperi-ci",
        ],
    )
    def test_a_value_naming_no_hyperi_ci_git_ref_is_not_a_hold(self, value) -> None:
        assert rehearse_branch.held_by(value) is None


def _sweep_runs(*statuses: str) -> str:
    return json.dumps(
        [{"databaseId": 100 + n, "status": s} for n, s in enumerate(statuses)]
    )


class TestARunningSweep:
    """A rehearsal's override reaches every run that starts while it is set.

    That includes the fixture runs a fleet sweep already dispatched, so the
    sweep would certify main on the rehearsed branch's CLI.
    """

    @staticmethod
    def _listing(monkeypatch, rc: int, stdout: str = "", stderr: str = "") -> list:
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(rehearse_branch, "_run", fake_run)
        return calls

    @pytest.mark.parametrize("status", ["queued", "in_progress"])
    def test_an_unfinished_sweep_is_found(self, monkeypatch, status) -> None:
        calls = self._listing(monkeypatch, 0, _sweep_runs("completed", status))
        assert rehearse_branch._running_sweep() == 101
        assert "fleet-sweep.yml" in calls[0]
        assert "hyperi-io/hyperi-ci" in calls[0]

    def test_finished_sweeps_are_not_running(self, monkeypatch) -> None:
        self._listing(monkeypatch, 0, _sweep_runs("completed", "completed"))
        assert rehearse_branch._running_sweep() is None

    def test_no_sweep_ever_is_not_running(self, monkeypatch) -> None:
        self._listing(monkeypatch, 0, "[]")
        assert rehearse_branch._running_sweep() is None

    def test_a_failed_query_is_not_read_as_no_sweep(self, monkeypatch) -> None:
        self._listing(monkeypatch, 1, stderr="gh: Bad credentials (HTTP 401)")
        with pytest.raises(rehearse_branch.SweepUnreadableError, match="HTTP 401"):
            rehearse_branch._running_sweep()

    def test_an_unreadable_answer_is_not_read_as_no_sweep(self, monkeypatch) -> None:
        self._listing(monkeypatch, 0, "<html>")
        with pytest.raises(rehearse_branch.SweepUnreadableError):
            rehearse_branch._running_sweep()


class _FixtureTouchedError(Exception):
    """Raised by a fake gh the moment a call reaches the fixture repo."""


class TestTheRehearsalRefusesDuringASweep:
    FIXTURE = "hyperi-io/ci-test-go-app"

    def _main(self, monkeypatch, sweep_listing, *extra: str) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(args)
            if any(self.FIXTURE in arg for arg in args):
                raise _FixtureTouchedError(args)
            if args[:2] == ["gh", "api"]:
                return subprocess.CompletedProcess(args, 0, stdout="a" * 40)
            return sweep_listing(args)

        monkeypatch.setattr(rehearse_branch, "_run", fake_run)
        argv = ["rehearse-branch.py", "--branch", "fix/x", "--repo", self.FIXTURE]
        monkeypatch.setattr(sys, "argv", [*argv, *extra])
        return calls

    def test_a_running_sweep_refuses_with_nothing_touched(
        self, monkeypatch, capsys
    ) -> None:
        self._main(
            monkeypatch,
            lambda a: subprocess.CompletedProcess(
                a,
                0,
                stdout=json.dumps([{"databaseId": 36001828234, "status": "queued"}]),
            ),
        )
        assert rehearse_branch.main() == 2
        assert "36001828234" in capsys.readouterr().out

    def test_a_failed_sweep_query_refuses_with_nothing_touched(
        self, monkeypatch, capsys
    ) -> None:
        self._main(
            monkeypatch,
            lambda a: subprocess.CompletedProcess(a, 1, stderr="gh: HTTP 502"),
        )
        assert rehearse_branch.main() == 1
        assert "HTTP 502" in capsys.readouterr().err

    def test_no_sweep_lets_the_rehearsal_reach_the_fixture(self, monkeypatch) -> None:
        self._main(
            monkeypatch,
            lambda a: subprocess.CompletedProcess(
                a, 0, stdout=_sweep_runs("completed")
            ),
        )
        with pytest.raises(_FixtureTouchedError):
            rehearse_branch.main()

    def test_without_the_override_a_sweep_is_not_in_the_way(self, monkeypatch) -> None:
        """No override is set, so nothing reaches the sweep's runs."""
        calls = self._main(
            monkeypatch,
            lambda a: subprocess.CompletedProcess(a, 0, stdout=_sweep_runs("queued")),
            "--no-cli-override",
        )
        with pytest.raises(_FixtureTouchedError):
            rehearse_branch.main()
        assert not any("fleet-sweep.yml" in args for args in calls)


_FIXTURE = "hyperi-io/ci-test-go-app"


def _fake_fixture(
    prior: str | None,
    *,
    pr_create_rc: int = 0,
    var_set_rc: int = 0,
    var_delete_rc: int = 0,
    rev_parse_rc: int = 0,
):
    """Answer every gh and git call main() makes, recording what it changes.

    Returns (fake _run, changes). A change is a variable set or delete, a push
    or branch delete, or a PR create or close.
    """
    changes: list[list[str]] = []

    def fake_run(args, **_kwargs):
        def ok(out: str = "") -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        if args[:3] == ["gh", "run", "list"]:
            return ok("[]")
        if args[:2] == ["gh", "api"] and "/branches/" in args[2]:
            return ok("a" * 40)
        if args[:2] == ["gh", "api"] and "/actions/variables/" in args[2]:
            if prior is None:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="gh: Not Found (HTTP 404)"
                )
            return ok(prior)
        if args[:3] == ["gh", "repo", "clone"]:
            workflows = Path(args[4]) / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "ci.yml").write_text(
                "uses: hyperi-io/hyperi-ci/.github/workflows/go-ci.yml@main\n",
                encoding="utf-8",
            )
            return ok()
        if args[:2] in (["gh", "variable"], ["gh", "pr"]):
            changes.append(args)
            if args[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(
                    args,
                    pr_create_rc,
                    stdout="https://github.com/hyperi-io/ci-test-go-app/pull/5",
                    stderr="gh: Validation Failed (HTTP 422)",
                )
            if args[:3] == ["gh", "variable", "set"]:
                return subprocess.CompletedProcess(
                    args, var_set_rc, stdout="", stderr="gh: HTTP 403"
                )
            if args[:3] == ["gh", "variable", "delete"]:
                return subprocess.CompletedProcess(
                    args, var_delete_rc, stdout="", stderr="gh: HTTP 502"
                )
            return ok()
        if args[0] == "git":
            if "push" in args:
                changes.append(args)
            if "rev-parse" in args:
                return subprocess.CompletedProcess(
                    args, rev_parse_rc, stdout="b" * 40, stderr="fatal: bad HEAD"
                )
            return ok()
        raise AssertionError(f"unexpected call: {args}")

    return fake_run, changes


def _events(changes: list[list[str]]) -> list[str]:
    """Each change as one word pair, in the order main() made it."""
    events = []
    for change in changes:
        if change[:2] in (["gh", "variable"], ["gh", "pr"]):
            events.append(f"{change[1]} {change[2]}")
        else:
            events.append("branch delete" if "--delete" in change else "branch push")
    return events


def _var_changes(changes: list[list[str]]) -> list[tuple[str, str]]:
    """(action, value) for each variable change, value empty for a delete."""
    return [
        (c[2], c[c.index("--body") + 1] if "--body" in c else "")
        for c in changes
        if c[:2] == ["gh", "variable"]
    ]


class TestTheRehearsalLeavesTheOverrideAsItFoundIt:
    """A leaked override outlives the rehearsal branch it names.

    Once that branch is merged and deleted, every run on the fixture fails at
    install, and a later rehearsal would record it as the value to put back.
    """

    @staticmethod
    def _run_main(monkeypatch, fake, *extra: str) -> int:
        monkeypatch.setattr(rehearse_branch, "_run", fake)
        argv = ["rehearse-branch.py", "--branch", "fix/x", "--repo", _FIXTURE]
        monkeypatch.setattr(sys, "argv", [*argv, *extra])
        return rehearse_branch.main()

    @pytest.mark.parametrize("holder", ["fix/other", "fix/x"])
    def test_a_fixture_another_rehearsal_holds_is_refused_untouched(
        self, monkeypatch, capsys, holder
    ) -> None:
        """Its own branch's stale override is a hold too: it was never put back."""
        fake, changes = _fake_fixture(rehearse_branch.override_value(holder))
        assert self._run_main(monkeypatch, fake) == 1
        assert changes == []
        assert holder in capsys.readouterr().err

    def test_the_permanent_main_pin_is_not_a_hold(self, monkeypatch) -> None:
        fake, changes = _fake_fixture(_MAIN_PIN, pr_create_rc=1)
        self._run_main(monkeypatch, fake)
        assert any(c[:3] == ["gh", "pr", "create"] for c in changes)

    def test_a_failed_pr_create_deletes_an_override_that_was_unset(
        self, monkeypatch
    ) -> None:
        fake, changes = _fake_fixture(None, pr_create_rc=1)
        assert self._run_main(monkeypatch, fake) == 1
        assert _var_changes(changes) == [
            ("set", rehearse_branch.override_value("fix/x")),
            ("delete", ""),
        ]

    def test_a_failed_pr_create_puts_a_prior_override_back(self, monkeypatch) -> None:
        fake, changes = _fake_fixture(_MAIN_PIN, pr_create_rc=1)
        assert self._run_main(monkeypatch, fake) == 1
        assert _var_changes(changes) == [
            ("set", rehearse_branch.override_value("fix/x")),
            ("set", _MAIN_PIN),
        ]

    def test_keep_leaves_the_override_in_place(self, monkeypatch) -> None:
        fake, changes = _fake_fixture(None, pr_create_rc=1)
        assert self._run_main(monkeypatch, fake, "--keep") == 1
        assert _var_changes(changes) == [
            ("set", rehearse_branch.override_value("fix/x"))
        ]

    def test_an_exception_after_the_override_is_set_still_restores_it(
        self, monkeypatch
    ) -> None:
        def watch(*_args, **_kwargs):
            raise RuntimeError("gh went away")

        fake, changes = _fake_fixture(None)
        monkeypatch.setattr(rehearse_branch, "_wait_for_merge_ref", lambda *_a: True)
        monkeypatch.setattr(rehearse_branch, "_watch_pr_run", watch)
        with pytest.raises(RuntimeError, match="gh went away"):
            self._run_main(monkeypatch, fake)
        assert _var_changes(changes)[-1] == ("delete", "")


class TestTheRehearsalBranchGoesOnEveryExit:
    """The sweep waits on any rehearse/* branch, so one left behind blocks the
    fixture in every sweep until someone deletes it by hand.

    The override goes back first, so the branch the sweep waits on outlives it.
    """

    @staticmethod
    def _run_main(monkeypatch, fake, *extra: str) -> int:
        monkeypatch.setattr(rehearse_branch, "_run", fake)
        argv = ["rehearse-branch.py", "--branch", "fix/x", "--repo", _FIXTURE]
        monkeypatch.setattr(sys, "argv", [*argv, *extra])
        return rehearse_branch.main()

    @pytest.mark.parametrize(
        ("knobs", "extra", "expected"),
        [
            ({"rev_parse_rc": 1}, (), ["branch push", "branch delete"]),
            (
                {"var_set_rc": 1},
                (),
                ["branch push", "variable set", "branch delete"],
            ),
            (
                {"pr_create_rc": 1},
                (),
                [
                    "branch push",
                    "variable set",
                    "pr create",
                    "variable delete",
                    "branch delete",
                ],
            ),
            (
                {"pr_create_rc": 1},
                ("--no-cli-override",),
                ["branch push", "pr create", "branch delete"],
            ),
        ],
        ids=["rev-parse", "override-set", "pr-create", "pr-create-no-override"],
    )
    def test_a_failure_after_the_push_deletes_the_branch_last(
        self, monkeypatch, knobs, extra, expected
    ) -> None:
        fake, changes = _fake_fixture(None, **knobs)
        assert self._run_main(monkeypatch, fake, *extra) == 1
        assert _events(changes) == expected

    def test_an_exception_restores_the_override_then_deletes_the_branch(
        self, monkeypatch
    ) -> None:
        def watch(*_args, **_kwargs):
            raise RuntimeError("gh went away")

        fake, changes = _fake_fixture(None)
        monkeypatch.setattr(rehearse_branch, "_wait_for_merge_ref", lambda *_a: True)
        monkeypatch.setattr(rehearse_branch, "_watch_pr_run", watch)
        with pytest.raises(RuntimeError, match="gh went away"):
            self._run_main(monkeypatch, fake)
        assert _events(changes)[-2:] == ["variable delete", "branch delete"]

    def test_a_pass_closes_the_pr_then_restores_then_deletes(
        self, monkeypatch, capsys
    ) -> None:
        fake, changes = _fake_fixture(_MAIN_PIN)
        monkeypatch.setattr(rehearse_branch, "_wait_for_merge_ref", lambda *_a: True)
        monkeypatch.setattr(
            rehearse_branch, "_watch_pr_run", lambda *_a: ("pass", [], 42)
        )
        monkeypatch.setattr(rehearse_branch, "_write_record", lambda *_a: True)
        assert self._run_main(monkeypatch, fake) == 0
        assert _events(changes) == [
            "branch push",
            "variable set",
            "pr create",
            "pr close",
            "variable set",
            "branch delete",
        ]
        assert "Cleaned up" in capsys.readouterr().out

    def test_keep_leaves_the_branch_and_the_override(self, monkeypatch) -> None:
        fake, changes = _fake_fixture(None, pr_create_rc=1)
        assert self._run_main(monkeypatch, fake, "--keep") == 1
        assert _events(changes) == ["branch push", "variable set", "pr create"]

    def test_a_failed_restore_keeps_the_branch(self, monkeypatch, capsys) -> None:
        """The branch is the only sign a branch CLI is still on the fixture."""
        fake, changes = _fake_fixture(None, pr_create_rc=1, var_delete_rc=1)
        assert self._run_main(monkeypatch, fake) == 1
        assert _events(changes) == [
            "branch push",
            "variable set",
            "pr create",
            "variable delete",
        ]
        assert "rehearse/fix-x KEPT" in capsys.readouterr().out
