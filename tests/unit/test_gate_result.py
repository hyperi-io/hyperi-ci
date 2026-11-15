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

import pytest

from hyperi_ci.gate_result import GateVerdict, TierContext, evaluate


@pytest.fixture(autouse=True)
def _no_gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that pass no tier must not pick one up from the runner's env."""
    for name in (
        "HYPERCI_GATE_TEST_TIER",
        "HYPERCI_GATE_WILL_RELEASE",
        "HYPERCI_GATE_FULL_REQUIRED",
    ):
        monkeypatch.delenv(name, raising=False)


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


_PR_RUN = {"run_checks": True, "run_build": False, "plan": "success"}
_BOTH_PASS = {"quality": "success", "test": "success"}


@pytest.mark.parametrize(
    ("kwargs", "needles"),
    [
        pytest.param(
            {
                **_PR_RUN,
                "run_build": True,
                "checks": _BOTH_PASS,
                "build": {"build": "skipped"},
            },
            ["build"],
            id="a-publish-run-needs-its-build",
        ),
        pytest.param(
            {**_PR_RUN, "checks": {"quality": "skipped", "test": "success"}},
            ["quality"],
            id="a-skipped-required-check",
        ),
        pytest.param(
            {**_PR_RUN, "checks": {"quality": "skipped", "test": "skipped"}},
            ["quality", "test"],
            id="every-absent-check-is-named",
        ),
        pytest.param(
            {
                "run_checks": False,
                "run_build": False,
                "plan": "skipped",
                "checks": {"quality": "skipped", "test": "skipped"},
            },
            ["decided nothing"],
            id="a-skipped-plan-fails-whatever-else-says",
        ),
        pytest.param(
            {**_PR_RUN, "checks": {"quality": "failure", "test": "success"}},
            ["quality"],
            id="a-failure",
        ),
        pytest.param(
            {**_PR_RUN, "checks": _BOTH_PASS, "build": {"build": "failure"}},
            ["build"],
            id="a-failed-build-even-when-not-required",
        ),
        pytest.param(
            {**_PR_RUN, "checks": {"test": "cancelled"}},
            [],
            id="a-cancelled-check",
        ),
        pytest.param(
            {**_PR_RUN, "checks": {"test": "timed_out"}},
            [],
            id="a-timeout",
        ),
        pytest.param(
            {
                "run_checks": False,
                "run_build": False,
                "plan": "failure",
                "checks": {"quality": "skipped", "test": "skipped"},
                "build": {"build": "skipped"},
            },
            ["failure"],
            id="a-failed-plan",
        ),
        pytest.param(
            {
                "run_checks": False,
                "run_build": False,
                "plan": "cancelled",
                "checks": {"quality": "skipped"},
            },
            [],
            id="a-cancelled-plan",
        ),
        pytest.param(
            {
                "run_checks": False,
                "run_build": False,
                "plan": "",
                "checks": {"quality": "skipped"},
            },
            ["nothing"],
            id="an-absent-plan-result",
        ),
        pytest.param(
            {**_PR_RUN, "checks": {}, "build": {}},
            ["env:"],
            id="required-checks-with-no-results",
        ),
        pytest.param(
            {**_PR_RUN, "run_build": True, "checks": _BOTH_PASS, "build": {}},
            ["build"],
            id="required-build-with-no-result",
        ),
        pytest.param(
            {**_PR_RUN, "checks": {"quality": "neutral", "test": "success"}},
            ["quality"],
            id="an-unknown-result-is-not-a-pass",
        ),
    ],
)
def test_the_gate_fails_and_says_why(
    kwargs: dict[str, object], needles: list[str]
) -> None:
    verdict = evaluate(**kwargs)
    assert not verdict.ok
    for needle in needles:
        assert needle in verdict.reason


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


def _passing_release(tier: TierContext) -> GateVerdict:
    return evaluate(
        run_checks=True,
        run_build=True,
        plan="success",
        checks={"quality": "success", "test": "success"},
        build={"build": "success"},
        tier=tier,
    )


class TestTheTestTier:
    """The Gate reports the tier; it fails only on a contradiction.

    It runs beside the release tail, so it cannot stop a release. The plan
    forces full on an opted-in release, so a lesser tier arriving here means
    the workflow is wired wrong, and the run is marked failed after the fact.
    """

    def test_an_opted_in_release_handed_core_fails_as_miswired(self) -> None:
        verdict = _passing_release(
            TierContext(tier="core", will_release=True, full_required=True)
        )
        assert not verdict.ok
        assert "tier core" in verdict.reason
        assert "test.full.required_for_release" in verdict.reason
        assert "wired wrong" in verdict.reason
        assert "refuse" not in verdict.reason

    def test_release_at_core_passes_without_the_opt_in_and_says_so(self) -> None:
        verdict = _passing_release(TierContext(tier="core", will_release=True))
        assert verdict.ok
        assert "tier core" in verdict.reason
        assert "is off" in verdict.reason

    def test_release_at_full_passes(self) -> None:
        verdict = _passing_release(
            TierContext(tier="full", will_release=True, full_required=True)
        )
        assert verdict.ok
        assert "tier full" in verdict.reason

    def test_an_opted_in_release_with_no_tier_fails(self) -> None:
        """A missing tier is not full."""
        verdict = _passing_release(
            TierContext(tier="", will_release=True, full_required=True)
        )
        assert not verdict.ok

    def test_pr_at_core_passes_with_the_tier_named(self) -> None:
        verdict = evaluate(
            run_checks=True,
            run_build=False,
            plan="success",
            checks={"quality": "success", "test": "success"},
            build={"build": "skipped"},
            tier=TierContext(tier="core", full_required=True),
        )
        assert verdict.ok
        assert "tier core" in verdict.reason
        assert "release" not in verdict.reason

    def test_a_failed_test_is_named_before_the_tier(self) -> None:
        verdict = evaluate(
            run_checks=True,
            run_build=True,
            plan="success",
            checks={"quality": "success", "test": "failure"},
            build={"build": "skipped"},
            tier=TierContext(tier="core", will_release=True, full_required=True),
        )
        assert not verdict.ok
        assert verdict.reason.startswith("Did not pass: test")

    def test_a_workflow_without_tiers_reads_as_before(self) -> None:
        verdict = _passing_release(TierContext())
        assert verdict.ok
        assert "tier" not in verdict.reason

    def test_a_doctrine_skip_names_no_tier(self) -> None:
        verdict = evaluate(
            run_checks=False,
            run_build=False,
            plan="success",
            checks={"quality": "skipped", "test": "skipped"},
            tier=TierContext(tier="core"),
        )
        assert verdict.ok
        assert "tier" not in verdict.reason


class TestTheTierComesFromTheGateJobEnv:
    """The CLI passes no tier, so evaluate reads the Gate job's env."""

    def test_every_variable_is_read(self) -> None:
        context = TierContext.from_env(
            {
                "HYPERCI_GATE_TEST_TIER": " Full ",
                "HYPERCI_GATE_WILL_RELEASE": "true",
                "HYPERCI_GATE_FULL_REQUIRED": "true",
            }
        )
        assert context == TierContext(
            tier="full", will_release=True, full_required=True
        )

    def test_absent_variables_read_as_no_tier(self) -> None:
        assert TierContext.from_env({}) == TierContext()

    def test_evaluate_reads_the_process_env_when_not_given_a_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_GATE_TEST_TIER", "core")
        monkeypatch.setenv("HYPERCI_GATE_WILL_RELEASE", "true")
        monkeypatch.setenv("HYPERCI_GATE_FULL_REQUIRED", "true")
        verdict = evaluate(
            run_checks=True,
            run_build=True,
            plan="success",
            checks={"quality": "success", "test": "success"},
            build={"build": "success"},
        )
        assert not verdict.ok
        assert "tier core" in verdict.reason
