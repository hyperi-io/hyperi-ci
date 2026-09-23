# Project:   HyperI CI
# File:      tests/unit/test_negative_cases.py
# Purpose:   Tests for the planted-failure runner
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The outcome a negative-case runner must never call green (issue #219).

A runner that reads a passing run as a passing case certifies that the gate
works while the gate is off, which is worse than having no runner at all. That
outcome -- `leaked` -- gets its own tests here, and so does the wrong-stage
one, because both arrive wearing a green tick from every other angle.
"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))


def _load(stem: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / stem)
    assert spec is not None and spec.loader is not None  # a real file always resolves
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


negative = _load("negative-cases.py", "negative_cases")
sweep = _load("sweep-fleet.py", "sweep_fleet")

CONTRACT = """
case: hadolint-error
patch: hadolint-error.patch
branch: expect-fail/hadolint-error
expect: fail
stage: quality
reason: hadolint
"""

CASE = negative.Case(
    fixture="ci-test-manifests",
    name="hadolint-error",
    patch="hadolint-error.patch",
    branch="expect-fail/hadolint-error",
    stage="quality",
    reason="hadolint",
)


_JOB_IDS = iter(range(1000, 9999))


def _job(
    name: str, conclusion: str, steps: list[tuple[str, str]] | None = None
) -> dict:
    return {
        "databaseId": next(_JOB_IDS),
        "name": name,
        "conclusion": conclusion,
        "steps": [{"name": n, "conclusion": c} for n, c in steps or []],
    }


def _logs(job: dict, text: str) -> dict[int, str]:
    return {int(job["databaseId"]): text}


class TestTheContract:
    def test_a_full_contract_reads_back(self) -> None:
        case = negative.parse_case("ci-test-manifests", "hadolint-error", CONTRACT)
        assert case == CASE

    def test_the_branch_defaults_to_the_case_name(self) -> None:
        case = negative.parse_case(
            "f", "schema-invalid", "expect: fail\npatch: p.patch\nstage: s\nreason: r\n"
        )
        assert isinstance(case, negative.Case)
        assert case.branch == "expect-fail/schema-invalid"

    def test_a_contract_missing_its_stage_is_refused(self) -> None:
        problem = negative.parse_case(
            "f", "c", "expect: fail\npatch: p.patch\nreason: r\n"
        )
        assert isinstance(problem, str) and "stage" in problem

    def test_a_case_that_expects_a_pass_is_not_a_negative_case(self) -> None:
        problem = negative.parse_case(
            "f", "c", "expect: pass\npatch: p.patch\nstage: s\nreason: r\n"
        )
        assert isinstance(problem, str) and "expects 'fail'" in problem

    def test_an_empty_contract_is_refused_rather_than_skipped(self) -> None:
        assert isinstance(negative.parse_case("f", "c", ""), str)

    def test_a_fixture_with_no_case_directory_is_a_problem(self, tmp_path) -> None:
        cases, problems = negative.read_cases("ci-test-go-app", tmp_path)
        assert cases == []
        assert problems and ".ci-negative" in problems[0]

    def test_a_contract_naming_a_missing_patch_is_a_problem(self, tmp_path) -> None:
        directory = tmp_path / negative.CASE_DIR
        directory.mkdir()
        (directory / "hadolint-error.yaml").write_text(CONTRACT, encoding="utf-8")
        cases, problems = negative.read_cases("ci-test-manifests", tmp_path)
        assert cases == []
        assert problems and "patch hadolint-error.patch is missing" in problems[0]

    def test_a_complete_case_directory_reads(self, tmp_path) -> None:
        directory = tmp_path / negative.CASE_DIR
        directory.mkdir()
        (directory / "hadolint-error.yaml").write_text(CONTRACT, encoding="utf-8")
        (directory / "hadolint-error.patch").write_text("diff", encoding="utf-8")
        cases, problems = negative.read_cases("ci-test-manifests", tmp_path)
        assert [case.case_id for case in cases] == ["ci-test-manifests/hadolint-error"]
        assert problems == []


class TestMatchingTheDeclaredStage:
    """GitHub reports a job's display name, never its YAML key."""

    def test_a_stage_matches_a_job_name(self) -> None:
        assert negative.stage_matches("quality", _job("ci / Quality", "failure"))

    def test_a_hyphenated_stage_matches_its_failed_step(self) -> None:
        job = _job(
            "k8s + IaC linting",
            "failure",
            [("Set up Helm", "success"), ("Lint manifests, charts and IaC", "failure")],
        )
        assert negative.stage_matches("lint-manifests", job)

    def test_a_step_that_did_not_fail_does_not_count(self) -> None:
        """A job that died before the gate ran did not run the gate."""
        job = _job(
            "k8s + IaC linting",
            "failure",
            [("Install hyperi-ci", "failure"), ("Lint manifests", "skipped")],
        )
        assert not negative.stage_matches("lint-manifests", job)

    def test_an_unrelated_job_does_not_match(self) -> None:
        assert not negative.stage_matches("quality", _job("ci / Build", "failure"))


class TestWhatARunProved:
    def _quality_failed(self) -> tuple[list[dict], dict]:
        quality = _job("ci / Quality", "failure", [("Run quality checks", "failure")])
        jobs = [
            _job("ci / Plan", "success"),
            quality,
            _job("ci / Gate", "failure", [("Report what actually ran", "failure")]),
        ]
        return jobs, quality

    def test_a_failure_at_the_declared_stage_for_the_declared_reason_passes(
        self,
    ) -> None:
        jobs, quality = self._quality_failed()
        state, _ = negative.classify(
            CASE, "failure", jobs, _logs(quality, "DL3004 hadolint error")
        )
        assert state == negative.PASS

    def test_a_run_that_passed_is_a_leak(self) -> None:
        """The dangerous outcome: the planted failure shipped green."""
        state, detail = negative.classify(
            CASE, "success", [_job("ci / Quality", "success")], {}
        )
        assert state == negative.LEAKED
        assert "green" in detail

    def test_a_leak_is_red_through_the_sweep_verdict(self) -> None:
        """The runner shares one verdict with the sweep, and it refuses a leak."""
        results = [sweep.Result(CASE.case_id, negative.LEAKED, "went green")]
        code, lines = sweep.sweep_verdict([CASE.case_id], results)
        assert code == 1
        assert any(negative.LEAKED in line for line in lines)

    def test_a_skipped_gate_reads_as_a_leak_not_a_pass(self) -> None:
        """run-checks=false skips Quality and the run goes green in seconds."""
        jobs = [_job("ci / Plan", "success"), _job("ci / Quality", "skipped")]
        state, _ = negative.classify(CASE, "success", jobs, {})
        assert state == negative.LEAKED

    def test_a_failure_somewhere_else_is_not_a_pass(self) -> None:
        build = _job("ci / Build", "failure", [("Run build", "failure")])
        state, detail = negative.classify(
            CASE, "failure", [build], _logs(build, "hadolint")
        )
        assert state == negative.WRONG_STAGE
        assert "ci / Build" in detail

    def test_the_right_stage_for_the_wrong_reason_is_not_a_pass(self) -> None:
        jobs, quality = self._quality_failed()
        state, _ = negative.classify(
            CASE, "failure", jobs, _logs(quality, "ruff found 3 errors")
        )
        assert state == negative.WRONG_REASON

    def test_the_reason_has_to_be_in_the_failed_stage_own_log(self) -> None:
        """A tool named in an unrelated job proves nothing about this gate."""
        jobs, _ = self._quality_failed()
        gate = jobs[2]
        state, _ = negative.classify(CASE, "failure", jobs, _logs(gate, "hadolint"))
        assert state == negative.UNREACHABLE

    def test_an_unreadable_log_is_inconclusive_rather_than_green(self) -> None:
        jobs, _ = self._quality_failed()
        state, _ = negative.classify(CASE, "failure", jobs, {})
        assert state == negative.UNREACHABLE

    def test_a_cancelled_run_proves_nothing(self) -> None:
        state, _ = negative.classify(CASE, "cancelled", [], {})
        assert state == negative.UNREACHABLE

    def test_a_failed_run_with_no_failed_job_proves_nothing(self) -> None:
        state, _ = negative.classify(
            CASE, "failure", [_job("ci / Quality", "success")], {}
        )
        assert state == negative.UNREACHABLE


class TestFindingThisCycleSRun:
    """`expect-fail/<case>` is reused every cycle, so the branch is ambiguous."""

    RUNS = [
        {"databaseId": 2, "headSha": "b" * 40},
        {"databaseId": 1, "headSha": "a" * 40},
    ]

    def test_the_run_for_the_commit_we_planted(self) -> None:
        assert negative.pick_run(self.RUNS, "a" * 40) == 1

    def test_a_previous_cycle_run_is_not_this_cycle_evidence(self) -> None:
        """The newest run on the branch is the wrong answer, not a near miss."""
        assert negative.pick_run(self.RUNS, "c" * 40) is None

    def test_no_runs_yet_reads_as_not_started(self) -> None:
        assert negative.pick_run([], "a" * 40) is None


class TestSelectingFixtures:
    def test_only_the_fixtures_that_declare_cases(self) -> None:
        fleet = [
            {"name": "ci-test-ts-app"},
            {"name": "ci-test-manifests", "negative_cases": True},
            {"name": "ci-test-go-app", "negative_cases": True},
        ]
        names = [entry["name"] for entry in negative.negative_fixtures(fleet)]
        assert names == ["ci-test-go-app", "ci-test-manifests"]

    def test_the_declared_fixtures_are_the_live_fleet(self) -> None:
        """Marking a fixture `negative_cases` is what puts it in this run."""
        import fixture_fleet

        declared = negative.negative_fixtures(fixture_fleet.load_fleet())
        assert declared, "no fixture declares negative_cases"
