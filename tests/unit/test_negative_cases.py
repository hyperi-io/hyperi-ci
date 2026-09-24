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

A patch that no longer applies is the same failure one step earlier: the case
plants nothing, so its run proves nothing while the catalogue still lists it
(issue #248). The softened `pending_release` verdict is tested against the same
line -- it must never come out as PASS.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

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


PATCH = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""

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

    @pytest.mark.parametrize(
        "branch", ["main", "release", "feature/x", "expect-fail/", " main "]
    )
    def test_a_branch_outside_expect_fail_is_refused(self, branch: str) -> None:
        """The runner pushes the defect there, then deletes the branch."""
        text = CONTRACT.replace(
            "branch: expect-fail/hadolint-error", f"branch: {branch!r}"
        )
        problem = negative.parse_case("ci-test-manifests", "hadolint-error", text)
        assert isinstance(problem, str) and "expect-fail/" in problem, problem

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

    def test_a_case_is_not_waiting_on_a_release_unless_it_says_so(self) -> None:
        case = negative.parse_case("ci-test-manifests", "hadolint-error", CONTRACT)
        assert isinstance(case, negative.Case)
        assert case.pending_release is False

    def test_a_case_can_declare_it_is_waiting_on_a_release(self) -> None:
        case = negative.parse_case("f", "c", CONTRACT + "pending_release: true\n")
        assert isinstance(case, negative.Case)
        assert case.pending_release is True

    def test_a_non_boolean_pending_release_is_refused(self) -> None:
        """`pending_release: "no"` is a truthy string, so it is not coerced."""
        problem = negative.parse_case("f", "c", CONTRACT + 'pending_release: "no"\n')
        assert isinstance(problem, str) and "pending_release" in problem


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
            CASE, "failure", jobs, _logs(quality, _HADOLINT_LOG)
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


def _err(message: str) -> str:
    """A hyperi-ci error() line as the job-log API returns it."""
    return f"2026-09-23T05:32:03Z ##[error][ERROR   ] hyperi_ci.common:error:217 -   {message}"


def _ok(level: str, message: str) -> str:
    return f"2026-09-23T05:32:03Z [{level:<8}] hyperi_ci.common:x:1 -   {message}"


# Excerpts of the failed Quality / linting job logs of the last real run of each
# case: ci-test-manifests 35875547921 and 35875558348, ci-test-rust-simple
# 35821804196, ci-test-go-app 35821074047.
_HADOLINT_LOG = "\n".join(
    [
        _ok("INFO", "hadolint: linting 1 Dockerfile(s)..."),
        "2026-09-23T14:38:40Z ##[error]Do not use sudo as it leads to unpredictable "
        "behavior. Use a tool like gosu to enforce root",
        _err("hadolint: 1 error-severity finding(s) must be fixed"),
    ]
)
_RUST_AUDIT_LOG = "\n".join(
    [
        _ok("SUCCESS", "clippy src (all): passed"),
        _err("cargo audit: failed"),
        _err("cargo deny: failed"),
        "2026-09-23T05:32:59Z ID:        RUSTSEC-2021-0003",
    ]
)
_GO_VULN_LOG = "\n".join(
    [
        _err("govulncheck: failed"),
        "2026-09-23T05:14:43Z Vulnerability #1: GO-2022-1059",
    ]
)
_SCHEMA_LOG = "\n".join(
    [
        _ok("INFO", "kubeconform: validating 3 manifest file(s)..."),
        _err("kubeconform: 1 invalid manifest(s) must be fixed"),
    ]
)

_RUST_CASE = negative.Case(
    fixture="ci-test-rust-simple",
    name="rust-cve-advisory",
    patch="rust-cve-advisory.patch",
    branch="expect-fail/rust-cve-advisory",
    stage="quality",
    reason="audit",
    advisory="RUSTSEC-2021-0003",
)
_GO_CASE = negative.Case(
    fixture="ci-test-go-app",
    name="go-cve-govulncheck",
    patch="go-cve-govulncheck.patch",
    branch="expect-fail/go-cve-govulncheck",
    stage="quality",
    reason="govulncheck",
    advisory="GO-2022-1059",
)
_SCHEMA_CASE = negative.Case(
    fixture="ci-test-manifests",
    name="schema-invalid",
    patch="schema-invalid.patch",
    branch="expect-fail/schema-invalid",
    stage="k8s + IaC linting",
    reason="kubeconform",
)


class TestTheReasonIsAFailureNotAName:
    """Every tool prints its own name when it passes, is disabled, or is skipped.

    So a declared tool merely appearing in the failed job's log proved nothing,
    and a run that failed on something else read as the gate firing.
    """

    @pytest.mark.parametrize(
        ("case", "log"),
        [
            (CASE, _HADOLINT_LOG),
            (_RUST_CASE, _RUST_AUDIT_LOG),
            (_GO_CASE, _GO_VULN_LOG),
            (_SCHEMA_CASE, _SCHEMA_LOG),
        ],
    )
    def test_each_real_case_log_still_passes(self, case, log) -> None:
        assert negative.missing_evidence(case, log) == ""

    def test_a_disabled_audit_beside_a_failed_deny_is_not_the_audit_gate(
        self,
    ) -> None:
        log = "\n".join(
            [
                _ok("INFO", "cargo audit: disabled"),
                _err("cargo deny: failed"),
                "ID:        RUSTSEC-2021-0003",
            ]
        )
        assert negative.missing_evidence(_RUST_CASE, log)

    def test_a_passed_audit_beside_a_failed_clippy_is_not_the_audit_gate(
        self,
    ) -> None:
        log = "\n".join(
            [_err("clippy src (all): failed"), _ok("SUCCESS", "cargo audit: passed")]
        )
        assert negative.missing_evidence(_RUST_CASE, log)

    def test_a_passed_hadolint_beside_a_failed_ruff_is_not_the_hadolint_gate(
        self,
    ) -> None:
        log = "\n".join([_ok("SUCCESS", "hadolint: passed"), _err("ruff: failed")])
        assert negative.missing_evidence(CASE, log)

    def test_a_tool_that_could_not_run_did_not_fire(self) -> None:
        log = _err("hadolint could not complete - failing the gate")
        assert negative.missing_evidence(CASE, log)

    def test_an_audit_failure_on_some_other_advisory_is_not_this_case(self) -> None:
        log = "\n".join([_err("cargo audit: failed"), "ID:        RUSTSEC-2099-0001"])
        assert "RUSTSEC-2021-0003" in negative.missing_evidence(_RUST_CASE, log)

    def test_the_contract_advisory_is_read(self) -> None:
        case = negative.parse_case("f", "c", CONTRACT + "advisory: GO-2022-1059\n")
        assert isinstance(case, negative.Case)
        assert case.advisory == "GO-2022-1059"

    def test_the_verdict_names_what_was_missing(self) -> None:
        quality = _job("ci / Quality", "failure", [("Run quality checks", "failure")])
        log = "\n".join([_ok("SUCCESS", "hadolint: passed"), _err("ruff: failed")])
        state, detail = negative.classify(
            CASE, "failure", [quality], _logs(quality, log)
        )
        assert state == negative.WRONG_REASON
        assert "hadolint" in detail


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


def _clone(tmp_path: Path, target: str) -> Path:
    """A clone-shaped tree holding one file and one case patch against it."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text(target, encoding="utf-8")
    directory = tmp_path / negative.CASE_DIR
    directory.mkdir()
    (directory / "x.patch").write_text(PATCH, encoding="utf-8")
    return tmp_path


PATCH_CASE = negative.Case(
    fixture="ci-test-rust-simple",
    name="moved-context",
    patch="x.patch",
    branch="expect-fail/moved-context",
    stage="quality",
    reason="audit",
)


class TestThePatchStillApplies:
    """A patch whose context has moved plants nothing, and nothing said so.

    `rust-cve-advisory.patch` sat in that state once the fixture's `src/main.rs`
    grew a hot path, and the catalogue still listed the case as proving the
    advisory gate.
    """

    def test_a_patch_that_still_applies_reads_clean(self, tmp_path) -> None:
        clone = _clone(tmp_path, "one\ntwo\nthree\n")
        assert negative.patch_applies(clone, PATCH_CASE) == ""

    def test_a_patch_whose_context_moved_names_the_file(self, tmp_path) -> None:
        clone = _clone(tmp_path, "ONE\ntwo\nthree\n")
        reason = negative.patch_applies(clone, PATCH_CASE)
        assert "a.txt" in reason
        assert "does not apply" in reason

    def test_a_missing_patch_is_not_silently_clean(self, tmp_path) -> None:
        clone = _clone(tmp_path, "one\ntwo\nthree\n")
        (clone / negative.CASE_DIR / "x.patch").unlink()
        assert negative.patch_applies(clone, PATCH_CASE) != ""

    def test_the_checker_reports_a_moved_patch_as_stale(self, tmp_path) -> None:
        fixtures = {
            PATCH_CASE.fixture: negative.Fixture(
                repo=f"hyperi-io/{PATCH_CASE.fixture}",
                clone=_clone(tmp_path, "ONE\ntwo\nthree\n"),
            )
        }
        results = negative.check_patches(fixtures, [PATCH_CASE])
        assert [result.state for result in results] == [negative.STALE_PATCH]

    def test_the_checker_reports_a_live_patch_as_a_pass(self, tmp_path) -> None:
        fixtures = {
            PATCH_CASE.fixture: negative.Fixture(
                repo=f"hyperi-io/{PATCH_CASE.fixture}",
                clone=_clone(tmp_path, "one\ntwo\nthree\n"),
            )
        }
        results = negative.check_patches(fixtures, [PATCH_CASE])
        assert [result.state for result in results] == [negative.PASS]

    def test_a_stale_patch_is_red_through_the_sweep_verdict(self) -> None:
        results = [
            sweep.Result(PATCH_CASE.case_id, negative.STALE_PATCH, "does not apply")
        ]
        code, lines = sweep.sweep_verdict([PATCH_CASE.case_id], results)
        assert code == 1
        assert any(negative.STALE_PATCH in line for line in lines)


class TestACaseWaitingOnARelease:
    """A fixture takes workflows from @main on merge, the CLI from PyPI on release.

    A case for a gate that has landed and not shipped therefore fails for a
    reason that says nothing about the gate. It may read INCONCLUSIVE, and it
    may never read PASS.
    """

    PENDING = negative.Case(
        fixture="ci-test-manifests",
        name="hadolint-error",
        patch="hadolint-error.patch",
        branch="expect-fail/hadolint-error",
        stage="quality",
        reason="hadolint",
        pending_release=True,
    )

    def test_a_leak_softens_to_pending_release(self) -> None:
        state, detail = negative.soften(self.PENDING, negative.LEAKED, "went green")
        assert state == negative.PENDING_RELEASE
        assert "pending_release" in detail

    def test_a_wrong_reason_softens_too(self) -> None:
        state, _ = negative.soften(self.PENDING, negative.WRONG_REASON, "no tool")
        assert state == negative.PENDING_RELEASE

    def test_pending_release_is_not_a_pass(self) -> None:
        """The whole point: inconclusive, never green."""
        results = [
            sweep.Result(self.PENDING.case_id, negative.PENDING_RELEASE, "waiting")
        ]
        code, lines = sweep.sweep_verdict([self.PENDING.case_id], results)
        assert code == 2
        assert any(negative.PENDING_RELEASE in line for line in lines)

    def test_one_pending_case_does_not_green_a_run_that_also_passed(self) -> None:
        results = [
            sweep.Result("ci-test-go-app/x", negative.PASS, "failed as declared"),
            sweep.Result(self.PENDING.case_id, negative.PENDING_RELEASE, "waiting"),
        ]
        code, _ = sweep.sweep_verdict(
            ["ci-test-go-app/x", self.PENDING.case_id], results
        )
        assert code == 2

    def test_a_case_that_did_not_declare_it_is_left_red(self) -> None:
        state, detail = negative.soften(CASE, negative.LEAKED, "went green")
        assert (state, detail) == (negative.LEAKED, "went green")

    def test_a_pending_case_that_passes_says_to_drop_the_flag(self) -> None:
        state, detail = negative.soften(
            self.PENDING, negative.PASS, "failed at quality"
        )
        assert state == negative.PASS
        assert "drop pending_release" in detail

    def test_an_unreadable_run_stays_unreachable(self) -> None:
        state, _ = negative.soften(self.PENDING, negative.UNREACHABLE, "no log")
        assert state == negative.UNREACHABLE

    def test_the_inconclusive_set_never_holds_a_pass(self) -> None:
        assert negative.PENDING_RELEASE in sweep.INCONCLUSIVE
        assert sweep.PASS not in sweep.INCONCLUSIVE

    def test_the_state_column_fits_every_state(self) -> None:
        """A state wider than the column turns the report into ragged prose."""
        states = [
            sweep.PASS,
            sweep.FAIL,
            sweep.TIMEOUT,
            sweep.UNREACHABLE,
            sweep.PENDING_RELEASE,
            sweep.HELD,
            negative.LEAKED,
            negative.WRONG_STAGE,
            negative.WRONG_REASON,
            negative.STALE_PATCH,
        ]
        results = [sweep.Result(f"f/{n}", state, "d") for n, state in enumerate(states)]
        _, lines = sweep.sweep_verdict([r.fixture for r in results], results)
        assert len({line.index(" f/") for line in lines}) == 1


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


_HADOLINT_FAILED = (
    "##[error][ERROR   ] hyperi_ci.common:error:217 -   "
    "hadolint: 1 error-severity finding(s) must be fixed"
)


class TestWaitingOnTheRun:
    """The poll runs for up to 45 minutes, so one bad read cannot end a case."""

    @staticmethod
    def _wait(
        monkeypatch, tmp_path, states: list, run_id: int = 7
    ) -> tuple[list, list[int]]:
        quality = _job("ci / Quality", "failure", [("Run quality checks", "failure")])
        reads: list[int] = []

        def run_state(_repo, run_id):
            reads.append(run_id)
            return states.pop(0) if states else None

        monkeypatch.setattr(negative.sweep_fleet, "_run_state", run_state)
        monkeypatch.setattr(negative.sweep_fleet, "_read_jobs", lambda *_a: [quality])
        monkeypatch.setattr(
            negative, "_failed_logs", lambda *_a: _logs(quality, _HADOLINT_FAILED)
        )
        monkeypatch.setattr(negative.time, "sleep", lambda _s: None)
        fixtures = {CASE.fixture: negative.Fixture(repo="o/r", clone=tmp_path)}
        live = [
            negative.Live(case=CASE, clone=tmp_path, head_sha="a" * 40, run_id=run_id)
        ]
        deadline = negative.time.time() + 60
        return negative._await_runs(fixtures, live, deadline), reads

    def test_one_failed_read_is_retried_not_a_verdict(
        self, monkeypatch, tmp_path
    ) -> None:
        results, _ = self._wait(monkeypatch, tmp_path, [None, ("completed", "failure")])
        assert [r.state for r in results] == [negative.PASS], results

    def test_the_run_judged_is_the_one_for_the_planted_commit(
        self, monkeypatch, tmp_path
    ) -> None:
        """The branch is reused every cycle, so its newest run can be stale.

        Goes through _pr_run_id's own gh call rather than pick_run alone: a
        selection rewired to "newest run on the branch" passes pick_run's
        tests and certifies a run this invocation never started.
        """
        listed = [
            {"databaseId": 222, "headSha": "b" * 40},
            {"databaseId": 111, "headSha": "a" * 40},
        ]

        def gh(args, **_kw):
            assert args[:3] == ["gh", "run", "list"], args
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(listed))

        monkeypatch.setattr(negative, "_run", gh)
        results, reads = self._wait(
            monkeypatch, tmp_path, [("completed", "failure")], run_id=0
        )
        assert reads == [111]
        assert results[0].detail.startswith("run 111:"), results

    def test_a_run_that_never_reads_gives_up_after_a_bounded_number(
        self, monkeypatch, tmp_path
    ) -> None:
        results, reads = self._wait(monkeypatch, tmp_path, [])
        assert [r.state for r in results] == [negative.UNREACHABLE]
        assert len(reads) == negative._RUN_READ_ATTEMPTS


class TestPinningTheCli:
    """ci-test-manifests carries a permanent HYPERCI_INSTALL_OVERRIDE.

    Cleanup deletes the variable when the prior value was None, so a read that
    FAILED must never be recorded as a read that found nothing.
    """

    def test_an_unreadable_override_is_never_set_or_deleted(
        self, monkeypatch, tmp_path
    ) -> None:
        class UnreadableError(Exception):
            """Stand-in for rehearse_branch.OverrideUnreadableError."""

        def read(repo):
            if repo == "o/manifests":
                raise UnreadableError("gh: Server Error (HTTP 502)")
            return None

        calls: list[tuple[str, str]] = []

        def gh_var(repo, action, value=""):
            calls.append((repo, action))
            return True

        rehearse = negative.rehearse_branch
        monkeypatch.setattr(
            rehearse, "OverrideUnreadableError", UnreadableError, raising=False
        )
        monkeypatch.setattr(rehearse, "_read_override", read)
        monkeypatch.setattr(rehearse, "_gh_var", gh_var)
        fixtures = {
            "ci-test-manifests": negative.Fixture(repo="o/manifests", clone=tmp_path),
            "ci-test-go-app": negative.Fixture(repo="o/go", clone=tmp_path),
        }
        go_case = negative.Case(
            fixture="ci-test-go-app",
            name="go-cve-govulncheck",
            patch="p.patch",
            branch="expect-fail/go-cve-govulncheck",
            stage="quality",
            reason="govulncheck",
        )

        runnable, results, fatal = negative._pin_fixtures(
            fixtures, [CASE, go_case], "fix/x"
        )
        negative._cleanup(fixtures, [])

        assert fatal == ""
        assert runnable == [go_case]
        assert [(r.fixture, r.state) for r in results] == [
            (CASE.case_id, negative.UNREACHABLE)
        ]
        assert "HTTP 502" in results[0].detail
        touched = [action for repo, action in calls if repo == "o/manifests"]
        assert touched == [], calls

    def test_a_fixture_a_rehearsal_holds_is_left_to_it(
        self, monkeypatch, tmp_path
    ) -> None:
        """Recorded as the prior, the rehearsal's branch would be put BACK.

        Once that rehearsal has finished and its branch is merged away, every
        run on the fixture then fails at install.
        """
        rehearse = negative.rehearse_branch
        priors = {
            "o/manifests": rehearse.override_value("fix/312-fetch-followups"),
            "o/go": None,
        }
        calls: list[tuple[str, str, str]] = []

        def gh_var(repo, action, value=""):
            calls.append((repo, action, value))
            return True

        monkeypatch.setattr(rehearse, "_read_override", priors.get)
        monkeypatch.setattr(rehearse, "_gh_var", gh_var)
        fixtures = {
            "ci-test-manifests": negative.Fixture(repo="o/manifests", clone=tmp_path),
            "ci-test-go-app": negative.Fixture(repo="o/go", clone=tmp_path),
        }
        go_case = negative.Case(
            fixture="ci-test-go-app",
            name="go-cve-govulncheck",
            patch="p.patch",
            branch="expect-fail/go-cve-govulncheck",
            stage="quality",
            reason="govulncheck",
        )

        runnable, results, fatal = negative._pin_fixtures(
            fixtures, [CASE, go_case], "fix/my-gate"
        )
        negative._cleanup(fixtures, [])

        assert fatal == ""
        assert runnable == [go_case]
        assert [(r.fixture, r.state) for r in results] == [
            (CASE.case_id, negative.UNREACHABLE)
        ]
        assert "fix/312-fetch-followups" in results[0].detail
        assert fixtures["ci-test-manifests"].prior_override is None
        assert [c for c in calls if c[0] == "o/manifests"] == [], calls
        # The free fixture is pinned and then put back as it was.
        assert [action for repo, action, _ in calls if repo == "o/go"] == [
            "set",
            "delete",
        ]


def _rehearse_refs(holds: dict[str, list[str] | str], reads: list[str]):
    """A fake gh answering the sweep's rehearse/* branch read per repo.

    A list is the branches on that repo, a string is a failed gh call.
    """

    def fake_run(args, **_kwargs):
        assert args[:2] == ["gh", "api"] and "matching-refs/heads/rehearse/" in args[2]
        repo = "/".join(args[2].split("/")[1:3])
        reads.append(repo)
        answer = holds[repo]
        if isinstance(answer, str):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=answer)
        refs = [{"ref": f"refs/heads/{branch}"} for branch in answer]
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(refs))

    return fake_run


class TestAFixtureARehearsalHolds:
    """A rehearsal's override reaches every run on the fixture, a case's too.

    The sweep job before this one already waited for the hold, so this one
    sets the fixture aside rather than waiting again.
    """

    SCHEMA = negative.Case(
        fixture="ci-test-manifests",
        name="schema-invalid",
        patch="schema-invalid.patch",
        branch="expect-fail/schema-invalid",
        stage="quality",
        reason="kubeconform",
    )

    def test_a_held_fixture_is_set_aside_once_read(self, monkeypatch, tmp_path) -> None:
        reads: list[str] = []
        monkeypatch.setattr(
            negative.sweep_fleet,
            "_run",
            _rehearse_refs(
                {
                    "hyperi-io/ci-test-manifests": ["rehearse/fix-x"],
                    "hyperi-io/ci-test-go-app": [],
                    "hyperi-io/ci-test-rust-simple": "gh: Server Error (HTTP 502)",
                },
                reads,
            ),
        )
        fixtures = {
            name: negative.Fixture(repo=f"hyperi-io/{name}", clone=tmp_path)
            for name in ("ci-test-manifests", "ci-test-go-app", "ci-test-rust-simple")
        }
        cases = [CASE, self.SCHEMA, _GO_CASE, _RUST_CASE]

        runnable, results = negative._set_aside_held(fixtures, cases)

        assert runnable == [_GO_CASE]
        by_case = {r.fixture: r for r in results}
        assert [by_case[c.case_id].state for c in (CASE, self.SCHEMA, _RUST_CASE)] == [
            negative.UNREACHABLE
        ] * 3
        assert "rehearse/fix-x" in by_case[CASE.case_id].detail
        assert "HTTP 502" in by_case[_RUST_CASE.case_id].detail
        # One read per fixture: no waiting.
        assert sorted(reads) == sorted(f.repo for f in fixtures.values())

    def test_the_cases_of_a_held_fixture_are_never_planted(
        self, monkeypatch, capsys
    ) -> None:
        prepared: list = []

        def prepare(_fixtures, cases):
            prepared.extend(cases)
            return [], []

        def read_cases(name, _clone):
            return [c for c in (CASE, _GO_CASE) if c.fixture == name], []

        monkeypatch.setattr(
            negative.fixture_fleet,
            "load_fleet",
            lambda: [
                {"name": "ci-test-manifests", "negative_cases": True},
                {"name": "ci-test-go-app", "negative_cases": True},
            ],
        )
        monkeypatch.setattr(negative, "_clone", lambda *_a, **_k: "")
        monkeypatch.setattr(negative, "read_cases", read_cases)
        monkeypatch.setattr(
            negative,
            "check_patches",
            lambda _f, cases: [
                sweep.Result(c.case_id, negative.PASS, "applies") for c in cases
            ],
        )
        monkeypatch.setattr(
            negative.sweep_fleet,
            "_run",
            _rehearse_refs(
                {
                    "hyperi-io/ci-test-manifests": ["rehearse/fix-x"],
                    "hyperi-io/ci-test-go-app": [],
                },
                [],
            ),
        )
        monkeypatch.setattr(negative, "_prepare", prepare)
        monkeypatch.setattr(negative, "_await_runs", lambda *_a: [])
        monkeypatch.setattr(negative, "_cleanup", lambda *_a: None)
        monkeypatch.setattr(sys, "argv", ["negative-cases.py"])

        assert negative.main() == 2
        assert prepared == [_GO_CASE]
        out = capsys.readouterr().out
        assert any(
            negative.UNREACHABLE in line and "rehearse/fix-x" in line
            for line in out.splitlines()
        ), out


class TestJobLogRead:
    """Reading a failed job's log is where this gate has always stopped (#264).

    A CI log carries ANSI colour, and a gh new enough to refuse an escaped
    body wants a flag an older gh does not have.
    """

    @staticmethod
    def _monkey(module, monkeypatch, fake) -> None:
        monkeypatch.setattr(module, "_run", fake)
        monkeypatch.setattr(module.time, "sleep", lambda _s: None)

    def test_the_escape_flag_is_added_only_after_gh_asks(self, monkeypatch) -> None:
        calls: list[list[str]] = []

        def adaptive(args, **_kw):
            calls.append(args)
            if "--allow-escape-sequences" not in args:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout="",
                    stderr="the response contains terminal escape sequences; "
                    "pass --allow-escape-sequences to output it anyway",
                )
            return subprocess.CompletedProcess(args, 0, stdout="hadolint DL3008")

        self._monkey(negative, monkeypatch, adaptive)
        text, why = negative._job_log("o/r", 1, attempts=1)
        assert text == "hadolint DL3008"
        # Learning the flag is not a failed read, so it must not spend the retry.
        assert len(calls) == 2

    def test_colour_is_stripped_so_the_reason_can_match(self, monkeypatch) -> None:
        body = "\x1b[31mhadolint\x1b[0m DL3008"
        self._monkey(
            negative,
            monkeypatch,
            lambda a, **_k: subprocess.CompletedProcess(a, 0, stdout=body),
        )
        assert negative._job_log("o/r", 1)[0] == "hadolint DL3008"

    def test_an_older_gh_without_the_flag_does_not_loop(self, monkeypatch) -> None:
        calls: list[list[str]] = []

        def refuses_both(args, **_kw):
            calls.append(args)
            if "--allow-escape-sequences" in args:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout="",
                    stderr="unknown flag: --allow-escape-sequences",
                )
            return subprocess.CompletedProcess(
                args,
                1,
                stdout="",
                stderr="pass --allow-escape-sequences to output it anyway",
            )

        self._monkey(negative, monkeypatch, refuses_both)
        text, why = negative._job_log("o/r", 1, attempts=2)
        assert text == ""
        assert "unknown flag" in why
        assert len(calls) <= 4

    def test_an_empty_body_is_named_separately_from_a_gh_failure(
        self, monkeypatch
    ) -> None:
        self._monkey(
            negative,
            monkeypatch,
            lambda a, **_k: subprocess.CompletedProcess(a, 0, stdout="   "),
        )
        assert "not published yet" in negative._job_log("o/r", 1, attempts=1)[1]
