# Project:   HyperI CI
# File:      tests/unit/test_gate_result.py
# Purpose:   Tests for the terminal gate verdict
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Gate verdict tests.

The point of the gate is that a SKIPPED check cannot satisfy it, so most of
these assert on skip cases rather than the passing one. The two gates are kept
apart deliberately: a build that skipped on a PR is CORRECT, and an early
version of this module failed every PR by treating build as run-checks
governed.
"""

from hyperi_ci.gate_result import evaluate


class TestTheTwoGatesAreSeparate:
    """run-checks governs quality and test; run-build governs build."""

    def test_a_pr_with_no_build_passes(self) -> None:
        """The case that caught the original bug.

        A PR has run-checks true and run-build false, because compiling a
        commit nothing ships is the line the doctrine draws. The build skips
        and that is correct.
        """
        verdict = evaluate(
            run_checks=True,
            run_build=False,
            plan="success",
            checks={"quality": "success", "test": "success"},
            build={"build": "skipped"},
        )
        assert verdict.ok

    def test_a_publish_run_needs_its_build(self) -> None:
        verdict = evaluate(
            run_checks=True,
            run_build=True,
            plan="success",
            checks={"quality": "success", "test": "success"},
            build={"build": "skipped"},
        )
        assert not verdict.ok
        assert "build" in verdict.reason


class TestTheDoctrineSkipIsAllowed:
    """A commit that ships nothing SHOULD skip quality and test."""

    def test_a_deliberate_skip_passes(self) -> None:
        verdict = evaluate(
            run_checks=False,
            run_build=False,
            plan="success",
            checks={"quality": "skipped", "test": "skipped"},
            build={"build": "skipped"},
        )
        assert verdict.ok

    def test_it_says_the_skip_was_deliberate(self) -> None:
        """The reason must survive being read by someone who did not run it."""
        verdict = evaluate(
            run_checks=False,
            run_build=False,
            plan="success",
            checks={"quality": "skipped"},
        )
        assert "doctrine" in verdict.reason
        assert "not missed" in verdict.reason


class TestARequiredCheckThatDidNotRun:
    """The defect this exists for: skipped and passed look identical."""

    def test_a_skipped_required_check_fails(self) -> None:
        verdict = evaluate(
            run_checks=True,
            run_build=False,
            plan="success",
            checks={"quality": "skipped", "test": "success"},
        )
        assert not verdict.ok
        assert "quality" in verdict.reason

    def test_every_absent_check_is_named(self) -> None:
        verdict = evaluate(
            run_checks=True,
            run_build=False,
            plan="success",
            checks={"quality": "skipped", "test": "skipped"},
        )
        assert "quality" in verdict.reason
        assert "test" in verdict.reason

    def test_a_skipped_plan_fails_whatever_else_says(self) -> None:
        """A fork PR used to land here -- no plan, no gate, everything skipped."""
        verdict = evaluate(
            run_checks=False,
            run_build=False,
            plan="skipped",
            checks={"quality": "skipped", "test": "skipped"},
        )
        assert not verdict.ok
        assert "decided nothing" in verdict.reason


class TestAFailedCheck:
    """GitHub shows these red already, but the gate must not disagree."""

    def test_a_failure_fails(self) -> None:
        verdict = evaluate(
            run_checks=True,
            run_build=False,
            plan="success",
            checks={"quality": "failure", "test": "success"},
        )
        assert not verdict.ok
        assert "quality" in verdict.reason

    def test_a_failed_build_fails_even_when_it_was_not_required(self) -> None:
        """A build that ran and broke is a failure whatever the gate asked for."""
        verdict = evaluate(
            run_checks=True,
            run_build=False,
            plan="success",
            checks={"quality": "success", "test": "success"},
            build={"build": "failure"},
        )
        assert not verdict.ok
        assert "build" in verdict.reason

    def test_a_cancelled_check_is_not_a_pass(self) -> None:
        """A cancelled required job has verified nothing."""
        verdict = evaluate(
            run_checks=True,
            run_build=False,
            plan="success",
            checks={"test": "cancelled"},
        )
        assert not verdict.ok

    def test_a_timeout_is_not_a_pass(self) -> None:
        verdict = evaluate(
            run_checks=True,
            run_build=False,
            plan="success",
            checks={"test": "timed_out"},
        )
        assert not verdict.ok


class TestTheHappyPath:
    def test_everything_ran_and_passed(self) -> None:
        verdict = evaluate(
            run_checks=True,
            run_build=True,
            plan="success",
            checks={"quality": "success", "test": "success"},
            build={"build": "success"},
        )
        assert verdict.ok
        assert "quality" in verdict.reason
        assert "build" in verdict.reason
