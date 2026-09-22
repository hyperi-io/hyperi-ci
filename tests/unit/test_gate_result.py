# Project:   HyperI CI
# File:      tests/unit/test_gate_result.py
# Purpose:   Tests for the terminal gate verdict
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Gate verdict tests.

The point of the gate is that a SKIPPED check cannot satisfy it, so most of
these assert on the skip cases rather than the passing one.
"""

from hyperi_ci.gate_result import evaluate


class TestTheDoctrineSkipIsAllowed:
    """A commit that ships nothing SHOULD skip quality and test."""

    def test_a_deliberate_skip_passes(self) -> None:
        verdict = evaluate(
            run_checks=False,
            plan="success",
            required={"quality": "skipped", "test": "skipped"},
        )
        assert verdict.ok

    def test_it_says_the_skip_was_deliberate(self) -> None:
        """The reason has to survive being read by someone who did not run it."""
        verdict = evaluate(
            run_checks=False,
            plan="success",
            required={"quality": "skipped"},
        )
        assert "doctrine" in verdict.reason
        assert "not missed" in verdict.reason


class TestARequiredCheckThatDidNotRun:
    """The defect this exists for: skipped and passed look identical."""

    def test_a_skipped_required_check_fails(self) -> None:
        verdict = evaluate(
            run_checks=True,
            plan="success",
            required={"quality": "skipped", "test": "success"},
        )
        assert not verdict.ok
        assert "quality" in verdict.reason

    def test_every_absent_check_is_named(self) -> None:
        verdict = evaluate(
            run_checks=True,
            plan="success",
            required={"quality": "skipped", "test": "skipped"},
        )
        assert "quality" in verdict.reason
        assert "test" in verdict.reason

    def test_a_skipped_plan_fails_whatever_else_says(self) -> None:
        """A fork PR used to land here -- no plan, no gate, everything skipped."""
        verdict = evaluate(
            run_checks=False,
            plan="skipped",
            required={"quality": "skipped", "test": "skipped"},
        )
        assert not verdict.ok
        assert "decided nothing" in verdict.reason


class TestAFailedCheck:
    """GitHub shows these red already, but the gate must not disagree."""

    def test_a_failure_fails(self) -> None:
        verdict = evaluate(
            run_checks=True,
            plan="success",
            required={"quality": "failure", "test": "success"},
        )
        assert not verdict.ok
        assert "quality" in verdict.reason

    def test_a_cancelled_check_is_not_a_pass(self) -> None:
        """A cancelled required job has verified nothing."""
        verdict = evaluate(
            run_checks=True,
            plan="success",
            required={"test": "cancelled"},
        )
        assert not verdict.ok

    def test_a_timeout_is_not_a_pass(self) -> None:
        verdict = evaluate(
            run_checks=True,
            plan="success",
            required={"test": "timed_out"},
        )
        assert not verdict.ok


class TestTheHappyPath:
    def test_everything_ran_and_passed(self) -> None:
        verdict = evaluate(
            run_checks=True,
            plan="success",
            required={"quality": "success", "test": "success"},
        )
        assert verdict.ok
        assert "quality" in verdict.reason
        assert "test" in verdict.reason
