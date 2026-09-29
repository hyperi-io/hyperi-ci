# Project:   HyperI CI
# File:      tests/unit/test_fleet_gate.py
# Purpose:   Tests for the rehearsal gate and the full-fleet sweep
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The two ways a fleet gate lies, and the tests that stop it (issue #215).

It reports success on having run NOTHING, or it treats a repo it could not
reach as a repo that is fine. Both look identical to a green tick from outside,
which is why each has its own test here rather than being folded into the
happy path.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import fixture_fleet  # noqa: E402


def _load(stem: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / stem)
    assert spec is not None and spec.loader is not None  # a real file always resolves
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load("rehearse-gate.py", "rehearse_gate")
sweep = _load("sweep-fleet.py", "sweep_fleet")
rehearse = _load("rehearse-branch.py", "rehearse_branch")


class TestTheRehearsalRecord:
    """What the rehearsal writes is what the gate reads back."""

    def test_round_trip(self) -> None:
        block = rehearse.record_block("a" * 40, 12345, "pass")
        record = rehearse.parse_record("body text" + block)
        assert record == {
            "hyperi-ci-sha": "a" * 40,
            "run-id": "12345",
            "verdict": "pass",
        }

    def test_a_body_without_a_record_reads_as_none(self) -> None:
        assert rehearse.parse_record("Throwaway rehearsal PR.") is None

    def test_an_empty_body_reads_as_none(self) -> None:
        assert rehearse.parse_record(None) is None
        assert rehearse.parse_record("") is None

    def test_a_truncated_record_reads_as_none(self) -> None:
        """Half a record is not a rehearsal."""
        assert rehearse.parse_record("Rehearsal record\nhyperi-ci-sha: abc\n") is None

    def test_the_newest_record_wins(self) -> None:
        body = (
            "x"
            + rehearse.record_block("a" * 40, 1, "fail")
            + rehearse.record_block("b" * 40, 2, "pass")
        )
        record = rehearse.parse_record(body)
        assert record is not None
        assert record["hyperi-ci-sha"] == "b" * 40

    def test_find_record_matches_the_commit_not_the_fixture(self) -> None:
        prs = [
            {"body": rehearse.record_block("a" * 40, 1, "pass")},
            {"body": rehearse.record_block("b" * 40, 2, "pass")},
        ]
        found = gate.find_record(prs, "b" * 40)
        assert found is not None and found["run-id"] == "2"

    def test_a_record_for_another_commit_does_not_count(self) -> None:
        """Push another commit and the rehearsal stops applying."""
        prs = [{"body": rehearse.record_block("a" * 40, 1, "pass")}]
        assert gate.find_record(prs, "c" * 40) is None


class TestTheGateVerdict:
    def test_nothing_required_is_a_pass(self) -> None:
        code, _ = gate.gate_verdict([], [])
        assert code == 0

    def test_all_proven_passes(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.PROVEN, "run 1")]
        assert gate.gate_verdict(["ci-test-go-app"], outcomes)[0] == 0

    def test_required_but_nothing_checked_is_not_a_pass(self) -> None:
        """The failure this gate exists for: a green tick over zero evidence."""
        code, lines = gate.gate_verdict(["ci-test-go-app"], [])
        assert code == 2
        assert any("NOT CHECKED" in line for line in lines)

    def test_one_fixture_answered_out_of_two_is_not_a_pass(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.PROVEN, "run 1")]
        code, lines = gate.gate_verdict(
            ["ci-test-go-app", "ci-test-rust-app"], outcomes
        )
        assert code == 2
        assert any("ci-test-rust-app" in line for line in lines)

    def test_an_unreachable_fixture_is_not_a_pass(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.UNREACHABLE, "no token")]
        assert gate.gate_verdict(["ci-test-go-app"], outcomes)[0] == 2

    def test_unreachable_outranks_unproven(self) -> None:
        """Unknown and known-bad are different answers; say the weaker one."""
        outcomes = [
            gate.Outcome("ci-test-go-app", gate.UNPROVEN, "no record"),
            gate.Outcome("ci-test-rust-app", gate.UNREACHABLE, "gh failed"),
        ]
        code, _ = gate.gate_verdict(["ci-test-go-app", "ci-test-rust-app"], outcomes)
        assert code == 2

    def test_an_unrehearsed_fixture_fails(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.UNPROVEN, "no record")]
        assert gate.gate_verdict(["ci-test-go-app"], outcomes)[0] == 1

    def test_a_failed_rehearsal_fails(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.FAILED, "run 9 did not pass")]
        assert gate.gate_verdict(["ci-test-go-app"], outcomes)[0] == 1

    def test_the_advice_names_the_command(self) -> None:
        lines = gate._advice("fix/thing", ["ci-test-go-app"])
        assert any(
            "rehearse-branch.py --branch fix/thing --repo hyperi-io/ci-test-go-app"
            in line
            for line in lines
        )


class TestACompositeChangeIsUnrehearsed:
    """The lang workflows call composites @main, so no rehearsal runs one (#366)."""

    def test_changed_composites_are_named_once_each(self) -> None:
        paths = [
            ".github/actions/predict-version/action.yml",
            "./.github/actions/predict-version/resolve.py",
            ".github/actions/setup-runtime/action.yml",
            ".github/workflows/python-ci.yml",
            "src/hyperi_ci/cli.py",
        ]
        assert gate.composite_changes(paths) == ["predict-version", "setup-runtime"]

    def test_no_composite_touched_names_none(self) -> None:
        assert gate.composite_changes([".github/workflows/python-ci.yml"]) == []

    def test_a_proven_rehearsal_does_not_prove_a_composite(self) -> None:
        outcomes = [gate.Outcome("ci-test-python-app", gate.PROVEN, "run 1")]
        code, lines = gate.gate_verdict(
            ["ci-test-python-app"], outcomes, ["predict-version"]
        )
        assert code == 1
        assert any(
            "UNREHEARSED" in line and "predict-version" in line for line in lines
        )

    def test_an_unknown_fixture_still_reads_as_unknown(self) -> None:
        outcomes = [gate.Outcome("ci-test-python-app", gate.UNREACHABLE, "gh failed")]
        code, _ = gate.gate_verdict(
            ["ci-test-python-app"], outcomes, ["predict-version"]
        )
        assert code == 2

    def test_a_composite_with_no_fixture_selected_is_not_a_pass(self) -> None:
        code, lines = gate.gate_verdict([], [], ["predict-version"])
        assert code == 1
        assert not any("No consumer surface" in line for line in lines)

    def _main(self, monkeypatch, changed: list[str]) -> int:
        argv = ["rehearse-gate.py", "--head-sha", "a" * 40, "--branch", "fix/x"]
        for path in changed:
            argv += ["--changed-file", path]
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(
            gate, "verify", lambda name, _sha: gate.Outcome(name, gate.PROVEN, "run 1")
        )
        return gate.main()

    def test_the_gate_refuses_a_proven_composite_change(
        self, monkeypatch, capsys
    ) -> None:
        code = self._main(monkeypatch, [".github/actions/predict-version/action.yml"])
        out = capsys.readouterr().out
        assert code == 1
        assert "COMPOSITE UNREHEARSED: predict-version" in out
        assert "REHEARSED: every required fixture" not in out

    def test_a_proven_workflow_change_still_passes(self, monkeypatch, capsys) -> None:
        code = self._main(monkeypatch, [".github/workflows/python-ci.yml"])
        assert code == 0
        assert "COMPOSITE UNREHEARSED" not in capsys.readouterr().out


class TestTheSweepVerdict:
    def test_a_green_fleet_passes(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.PASS, "run 1")]
        assert sweep.sweep_verdict(["ci-test-go-app"], results)[0] == 0

    def test_selecting_nothing_is_not_a_pass(self) -> None:
        code, lines = sweep.sweep_verdict([], [])
        assert code == 2
        assert any("0 fixtures" in line for line in lines)

    def test_running_nothing_is_not_a_pass(self) -> None:
        """A sweep that dispatched nothing has proven nothing about main."""
        code, lines = sweep.sweep_verdict(["ci-test-go-app"], [])
        assert code == 2
        assert any("0 fixtures" in line for line in lines)

    def test_a_fixture_with_no_answer_is_not_a_pass(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.PASS, "run 1")]
        code, lines = sweep.sweep_verdict(
            ["ci-test-go-app", "ci-test-rust-app"], results
        )
        assert code == 2
        assert any("NOT RUN" in line and "ci-test-rust-app" in line for line in lines)

    def test_an_unreachable_repo_is_not_a_pass(self) -> None:
        results = [
            sweep.Result("ci-test-go-app", sweep.PASS, "run 1"),
            sweep.Result("ci-test-rust-app", sweep.UNREACHABLE, "dispatch refused"),
        ]
        code, _ = sweep.sweep_verdict(["ci-test-go-app", "ci-test-rust-app"], results)
        assert code == 2

    def test_a_failing_fixture_is_a_red_fleet(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.FAIL, "run 1")]
        assert sweep.sweep_verdict(["ci-test-go-app"], results)[0] == 1

    def test_a_timed_out_run_is_a_red_fleet(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.TIMEOUT, "run 1 still going")]
        assert sweep.sweep_verdict(["ci-test-go-app"], results)[0] == 1

    def test_a_failure_outranks_an_unreachable_neighbour(self) -> None:
        """A proven failure is an answer whatever else could not be read.

        Reported as inconclusive, negative-cases prints "it did not prove any
        gate" over a gate it just proved leaks.
        """
        results = [
            sweep.Result("ci-test-go-app", sweep.FAIL, "run 1"),
            sweep.Result("ci-test-rust-app", sweep.UNREACHABLE, "gone"),
        ]
        code, lines = sweep.sweep_verdict(
            ["ci-test-go-app", "ci-test-rust-app"], results
        )
        assert code == 1
        assert any("unreachable" in line for line in lines)

    def test_a_failure_outranks_a_fixture_that_never_ran(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.FAIL, "run 1")]
        code, lines = sweep.sweep_verdict(
            ["ci-test-go-app", "ci-test-rust-app"], results
        )
        assert code == 1
        assert any("NOT RUN" in line and "ci-test-rust-app" in line for line in lines)

    def test_a_held_fixture_is_not_a_pass(self) -> None:
        results = [
            sweep.Result("ci-test-go-app", sweep.PASS, "run 1"),
            sweep.Result("ci-test-rust-app", sweep.HELD, "held by fix/x"),
        ]
        code, _ = sweep.sweep_verdict(["ci-test-go-app", "ci-test-rust-app"], results)
        assert code == 2
        assert sweep.HELD in sweep.INCONCLUSIVE


# ci-test-manifests run 36515341852 as `gh run view --json jobs` returns it:
# green, with a non-reusable job and two skipped ones. The sweep called it red.
_RUN_401 = json.loads(
    (Path(__file__).parent / "data" / "fleet-sweep-run-36515341852.json").read_text(
        encoding="utf-8"
    )
)


def _settling(jobs: list[dict]) -> list[dict]:
    """The same jobs with the last one read before it carries a conclusion."""
    *done, last = (dict(job) for job in jobs)
    return [*done, {**last, "status": "queued", "conclusion": ""}]


class TestReadingOneFixtureRun:
    """A fixture run's verdict is the run's own conclusion (issue #401)."""

    def test_the_401_run_passes(self) -> None:
        result = sweep.run_result(
            "ci-test-manifests", 36515341852, _RUN_401["conclusion"], None
        )
        assert result == sweep.Result(
            "ci-test-manifests", sweep.PASS, "run 36515341852"
        )

    def test_a_job_still_settling_does_not_turn_a_green_run_red(self) -> None:
        jobs = _settling(_RUN_401["jobs"])
        # The job-by-job reading the sweep used to apply fails this shape.
        assert sweep.rehearse_branch.summarise_jobs(jobs)[0] is False
        result = sweep.run_result("ci-test-manifests", 1, "success", jobs)
        assert result.state == sweep.PASS

    def test_a_failed_run_names_the_job_that_failed(self) -> None:
        jobs = [dict(job) for job in _RUN_401["jobs"]]
        container = next(job for job in jobs if job["name"].endswith("Container"))
        container["conclusion"] = "failure"
        result = sweep.run_result("ci-test-manifests", 7, "failure", jobs)
        assert result.state == sweep.FAIL
        assert result.detail == (
            "run 7 concluded failure: ci / Release tail / Container failure"
        )

    def test_a_cancelled_run_is_not_a_pass(self) -> None:
        result = sweep.run_result("ci-test-manifests", 7, "cancelled", [])
        assert result == sweep.Result(
            "ci-test-manifests", sweep.FAIL, "run 7 concluded cancelled"
        )

    def test_unreadable_jobs_still_fail_a_failed_run(self) -> None:
        result = sweep.run_result("ci-test-manifests", 7, "failure", None)
        assert result.state == sweep.FAIL
        assert "could not be read" in result.detail

    def test_a_null_conclusion_reads_as_none_not_the_word(self, monkeypatch) -> None:
        answer = json.dumps({"status": "in_progress", "conclusion": None})
        monkeypatch.setattr(
            sweep,
            "_run",
            lambda args, **_k: subprocess.CompletedProcess(args, 0, stdout=answer),
        )
        assert sweep._run_state("o/r", 1) == ("in_progress", "")


class TestPollingOneFixtureRun:
    """The sweep loop reads a verdict only once the run carries a conclusion."""

    @staticmethod
    def _sweep(monkeypatch, states: list[tuple[str, str]], jobs: list[dict]):
        clock = _Clock()
        answers = iter(states)
        job_reads: list[int] = []

        def read_jobs(_repo, run_id):
            job_reads.append(run_id)
            return jobs

        monkeypatch.setattr(sweep, "time", clock)
        monkeypatch.setattr(sweep, "_rehearsal_branches", lambda _repo: [])
        monkeypatch.setattr(sweep, "_start", lambda _name, _deadline: 36515341852)
        monkeypatch.setattr(sweep, "_run_state", lambda *_a: next(answers))
        monkeypatch.setattr(sweep, "_read_jobs", read_jobs)
        results = sweep._sweep([{"name": "ci-test-manifests"}], 5)
        return results, job_reads

    def test_the_401_run_passes_whatever_its_jobs_say(self, monkeypatch) -> None:
        results, job_reads = self._sweep(
            monkeypatch,
            [("in_progress", ""), ("completed", _RUN_401["conclusion"])],
            _settling(_RUN_401["jobs"]),
        )
        assert [r.state for r in results] == [sweep.PASS]
        # A green run needs no job read, so none can disagree with it.
        assert job_reads == []

    def test_completed_with_no_conclusion_is_still_going(self, monkeypatch) -> None:
        results, _ = self._sweep(
            monkeypatch,
            [("completed", ""), ("completed", "success")],
            [],
        )
        assert [r.state for r in results] == [sweep.PASS]

    def test_a_failed_run_is_read_for_its_jobs(self, monkeypatch) -> None:
        failed = [
            {"name": "ci / Quality", "status": "completed", "conclusion": "failure"}
        ]
        results, job_reads = self._sweep(
            monkeypatch, [("completed", "failure")], failed
        )
        assert [r.state for r in results] == [sweep.FAIL]
        assert "ci / Quality failure" in results[0].detail
        assert job_reads == [36515341852]


class _Clock:
    """Stands in for the sweep's `time` module: sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


_REHEARSAL = ["rehearse/fix-312-fetch-followups"]
_FAILED = "gh: Server Error (HTTP 502)"


def _matching_refs(branches: list[str]) -> str:
    """The body GitHub's matching-refs endpoint returns for these branches."""
    return json.dumps(
        [
            {
                "ref": f"refs/heads/{branch}",
                "node_id": "REF_x",
                "url": f"https://api.github.com/repos/o/r/git/refs/heads/{branch}",
                "object": {"sha": "c" * 40, "type": "commit", "url": "u"},
            }
            for branch in branches
        ]
    )


def _refs_answer(args: list[str], answer) -> subprocess.CompletedProcess:
    """A branch list, a failed gh call (a string), or an unreadable body (bytes)."""
    if isinstance(answer, str):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=answer)
    if isinstance(answer, bytes):
        return subprocess.CompletedProcess(args, 0, stdout=answer.decode())
    return subprocess.CompletedProcess(args, 0, stdout=_matching_refs(answer))


class TestReadingARehearsalHold:
    """A rehearsal's branch is pushed before its override is set, and deleted
    after the override is put back, so the branch covers the whole hold."""

    def _read(self, monkeypatch, answer) -> list[str]:
        asked: list[list[str]] = []

        def fake_run(args, **_kwargs):
            asked.append(args)
            return _refs_answer(args, answer)

        monkeypatch.setattr(sweep, "_run", fake_run)
        branches = sweep._rehearsal_branches("hyperi-io/ci-test-go-app")
        assert asked == [
            [
                "gh",
                "api",
                "repos/hyperi-io/ci-test-go-app/git/matching-refs/heads/rehearse/",
            ]
        ]
        return branches

    def test_each_rehearse_branch_is_named(self, monkeypatch) -> None:
        both = ["rehearse/fix-312-fetch-followups", "rehearse/fix-x"]
        assert self._read(monkeypatch, both) == both

    def test_no_rehearse_branch_is_no_hold(self, monkeypatch) -> None:
        assert self._read(monkeypatch, []) == []

    def test_a_failed_read_is_not_read_as_no_hold(self, monkeypatch) -> None:
        with pytest.raises(sweep.HoldUnreadableError, match="HTTP 502"):
            self._read(monkeypatch, _FAILED)

    def test_an_unreadable_answer_is_not_read_as_no_hold(self, monkeypatch) -> None:
        with pytest.raises(sweep.HoldUnreadableError):
            self._read(monkeypatch, b"<html>")


class TestAFixtureARehearsalHolds:
    """A rehearsal points a fixture's CLI at its branch, repo-wide.

    Every run that starts meanwhile installs that branch, so a sweep that
    dispatched into it would certify main on a CLI main never shipped.
    """

    @staticmethod
    def _sweep(monkeypatch, holds: dict[str, list], minutes: int = 1):
        """Run `_sweep` over fake gh, popping each fixture's branch reads in turn.

        Returns (results, dispatch times by fixture, hold reads by fixture).
        """
        clock = _Clock()
        dispatched: dict[str, float] = {}
        reads: dict[str, int] = {}

        def fake_run(args, **_kwargs):
            name = args[2].split("/")[2]
            reads[name] = reads.get(name, 0) + 1
            queue = holds[name]
            return _refs_answer(args, queue.pop(0) if len(queue) > 1 else queue[0])

        def read_override(repo):
            raise AssertionError(f"the sweep read {repo}'s install override")

        def run_ids(repo):
            name = repo.rsplit("/", 1)[-1]
            return {7} if name in dispatched else set()

        def dispatch(repo):
            dispatched[repo.rsplit("/", 1)[-1]] = clock.now
            return ""

        monkeypatch.setattr(sweep, "time", clock)
        monkeypatch.setattr(sweep, "_run", fake_run)
        monkeypatch.setattr(sweep.rehearse_branch, "_read_override", read_override)
        monkeypatch.setattr(sweep, "_run_ids", run_ids)
        monkeypatch.setattr(sweep, "_dispatch", dispatch)
        monkeypatch.setattr(sweep, "_run_state", lambda *_a: ("completed", "success"))
        monkeypatch.setattr(
            sweep, "_read_jobs", lambda *_a: [{"name": "ci", "conclusion": "success"}]
        )
        targets = [{"name": name} for name in holds]
        results = sweep._sweep(targets, minutes)
        return {r.fixture: r for r in results}, dispatched, reads

    def test_a_hold_that_clears_is_swept_once_it_does(self, monkeypatch) -> None:
        results, dispatched, reads = self._sweep(
            monkeypatch, {"ci-test-go-app": [_REHEARSAL, _REHEARSAL, []]}
        )
        assert results["ci-test-go-app"].state == sweep.PASS
        # Read three times, dispatched on the third, one poll apart each.
        assert reads["ci-test-go-app"] == 3
        assert dispatched == {"ci-test-go-app": 2 * sweep._POLL_SECONDS}

    def test_a_hold_that_outlasts_the_deadline_is_never_dispatched(
        self, monkeypatch
    ) -> None:
        results, dispatched, _ = self._sweep(
            monkeypatch, {"ci-test-go-app": [_REHEARSAL]}
        )
        held = results["ci-test-go-app"]
        assert held.state == sweep.HELD
        assert "rehearse/fix-312-fetch-followups" in held.detail
        assert dispatched == {}

    def test_a_free_fixture_does_not_wait_behind_a_held_one(self, monkeypatch) -> None:
        results, dispatched, _ = self._sweep(
            monkeypatch,
            {
                "ci-test-a-held": [_REHEARSAL],
                "ci-test-b-freed": [_REHEARSAL, []],
                "ci-test-c-free": [[]],
            },
        )
        assert dispatched == {
            "ci-test-c-free": 0.0,
            "ci-test-b-freed": sweep._POLL_SECONDS,
        }
        assert [results[n].state for n in sorted(results)] == [
            sweep.HELD,
            sweep.PASS,
            sweep.PASS,
        ]
        code, _ = sweep.sweep_verdict(sorted(results), list(results.values()))
        assert code == 2

    def test_an_unreadable_hold_is_unreachable_not_dispatched(
        self, monkeypatch
    ) -> None:
        results, dispatched, _ = self._sweep(
            monkeypatch, {"ci-test-go-app": [_FAILED], "ci-test-rust-app": [[]]}
        )
        assert results["ci-test-go-app"].state == sweep.UNREACHABLE
        assert "HTTP 502" in results["ci-test-go-app"].detail
        assert dispatched == {"ci-test-rust-app": 0.0}


class TestSelectingSweepTargets:
    FLEET = [
        {"name": "ci-test-rs-app", "language": "rust"},
        {"name": "ci-test-go-app", "language": "go"},
        {"name": "ci-test-py-app", "language": "python"},
    ]

    def test_the_whole_fleet_by_default_in_name_order(self) -> None:
        names = [e["name"] for e in sweep.select_targets(self.FLEET, [], "")]
        assert names == ["ci-test-go-app", "ci-test-py-app", "ci-test-rs-app"]

    def test_one_language(self) -> None:
        names = [e["name"] for e in sweep.select_targets(self.FLEET, [], "rust")]
        assert names == ["ci-test-rs-app"]

    def test_an_explicit_list(self) -> None:
        names = [
            e["name"] for e in sweep.select_targets(self.FLEET, ["ci-test-go-app"], "")
        ]
        assert names == ["ci-test-go-app"]

    def test_a_filter_matching_nobody_returns_empty_rather_than_the_fleet(self) -> None:
        """Returning everything on an unmatched filter would sweep the fleet by
        accident; returning empty lets the verdict refuse it."""
        assert sweep.select_targets(self.FLEET, [], "cobol") == []


class TestThePushFilterIsTheConsumerSurface:
    """The sweep's `paths:` filter and config/fixtures.yaml have to agree about
    which workflows a consumer actually runs."""

    def _sweep_workflow(self) -> dict:
        text = (_ROOT / ".github" / "workflows" / "fleet-sweep.yml").read_text("utf-8")
        return yaml.safe_load(text)

    def test_the_exclusions_are_exactly_the_non_consumer_workflows(self) -> None:
        fleet = fixture_fleet.load_fleet()
        present = {p.name for p in (_ROOT / ".github" / "workflows").glob("*.yml")}
        consumed = fixture_fleet.language_workflows(fleet) | {
            name for name in present if name.startswith("_")
        }
        # `on` parses as the boolean True in YAML 1.1, which is what PyYAML is.
        paths = self._sweep_workflow()[True]["push"]["paths"]
        excluded = {
            p.removeprefix("!.github/workflows/") for p in paths if p.startswith("!")
        }
        assert excluded == present - consumed

    def test_the_filter_covers_both_surfaces(self) -> None:
        paths = self._sweep_workflow()[True]["push"]["paths"]
        assert ".github/workflows/**" in paths
        assert ".github/actions/**" in paths

    def test_every_app_token_is_scoped_to_named_repos(self) -> None:
        """With `owner` set and no `repositories`, the token reaches the org."""
        token_steps = [
            step
            for job in self._sweep_workflow()["jobs"].values()
            for step in job["steps"]
            if "create-github-app-token" in step.get("uses", "")
        ]
        assert token_steps
        for step in token_steps:
            assert "steps.fleet.outputs.repos" in step["with"].get("repositories", "")

    def test_no_dispatch_input_is_spliced_into_a_shell_script(self) -> None:
        for job in self._sweep_workflow()["jobs"].values():
            for step in job["steps"]:
                assert "${{ inputs." not in step.get("run", ""), step.get("name")


class TestTheTokenScope:
    """Which fixture repos a fleet workflow's app token may reach."""

    def test_the_sweep_reaches_the_whole_fleet(self) -> None:
        fleet = fixture_fleet.load_fleet()
        assert fixture_fleet.token_scope(fleet) == [e["name"] for e in fleet]

    def test_negative_cases_reach_only_their_fixtures(self) -> None:
        fleet = fixture_fleet.load_fleet()
        scope = fixture_fleet.token_scope(fleet, negative_cases_only=True)
        assert scope
        assert set(scope) == {e["name"] for e in fleet if e.get("negative_cases")}

    def test_an_empty_scope_is_refused_not_passed_on(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(fixture_fleet, "load_fleet", lambda: [])
        monkeypatch.setattr(sys, "argv", ["fixture_fleet.py"])
        assert fixture_fleet.main() == 1
        assert "repos=" not in capsys.readouterr().out
