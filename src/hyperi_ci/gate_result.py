# Project:   HyperI CI
# File:      src/hyperi_ci/gate_result.py
# Purpose:   Decide whether a run may report success given what actually ran
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Turn "what actually ran" into a status context branch protection can require.

A run whose every gate skipped still concludes ``success``, and GitHub counts a
SKIPPED required check as satisfied, so a branch-protection rule naming
``ci / Quality`` is satisfied by Quality not running (issue #177).

:mod:`hyperi_ci.gate_audit` reports that fleet-wide after the fact. This asks it
inside the run, where it can still stop a merge: did the jobs the plan's gate
required actually execute?

A push that ships nothing SHOULD skip quality and test and this passes it, with
the reason on the record.

It also names the test tier the plan resolved, as a report and not the
enforcement. The plan forces full on a release in a project that sets
``test.full.required_for_release``, the Test job passes ``--tier full`` to a CLI
that fails without it, and Build needs Test, so a release that owed full cannot
reach publishing on less. The Gate runs beside the release tail and cannot stop
it, so it fails only when the tier it was handed contradicts that (a wiring
fault).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

SUCCESS = "success"

# The job ran and did not pass. `cancelled` counts, as it verified nothing.
_FAILED = ("failure", "cancelled", "timed_out")


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """The decision, and the sentence explaining it.

    Attributes:
        ok: Whether the run may report success.
        reason: One line naming what ran, or what did not.

    """

    ok: bool
    reason: str


FULL_TIER = "full"


@dataclass(frozen=True, slots=True)
class TierContext:
    """The test tier the plan resolved, and whether it had to be full.

    Attributes:
        tier: ``core`` or ``full``; empty when the workflow predates tiers.
        will_release: Whether the plan decided this run tags and publishes.
        full_required: Whether the project set
            ``test.full.required_for_release``.

    """

    tier: str = ""
    will_release: bool = False
    full_required: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Read the context the Gate job's ``env:`` block passes.

        Args:
            environ: The environment to read; the process environment if None.

        Returns:
            The context. Absent variables read as a workflow that predates
            tiers, which changes nothing about the verdict.

        """
        env = os.environ if environ is None else environ
        return cls(
            tier=env.get("HYPERCI_GATE_TEST_TIER", "").strip().lower(),
            will_release=env.get("HYPERCI_GATE_WILL_RELEASE", "") == "true",
            full_required=env.get("HYPERCI_GATE_FULL_REQUIRED", "") == "true",
        )

    @property
    def release_short_of_full(self) -> bool:
        """Whether a release that owed full was handed a lesser tier."""
        return self.will_release and self.full_required and self.tier != FULL_TIER

    def describe(self) -> str:
        """Return the sentence naming the tier, empty when there is none."""
        if not self.tier:
            return ""
        if self.will_release and self.tier != FULL_TIER:
            return (
                f" Tests ran at tier {self.tier} on a release; "
                f"test.full.required_for_release is off."
            )
        return f" Tests ran at tier {self.tier}."


def evaluate(
    *,
    run_checks: bool,
    run_build: bool,
    plan: str,
    checks: dict[str, str],
    build: dict[str, str] | None = None,
    tier: TierContext | None = None,
) -> GateVerdict:
    """Decide whether a run may report success.

    ``run_checks`` governs quality and test. ``run_build`` governs build and is
    publish-only, so a normal PR has run-checks true and run-build false and a
    skipped build there is correct.

    Args:
        run_checks: Whether the doctrine required quality and test here.
        run_build: Whether the doctrine required a build here.
        plan: Result of the plan job. A skipped plan decided nothing, so
              nothing downstream can be trusted.
        checks: Job name to result, for the jobs ``run_checks`` governs.
        build: Job name to result, for the jobs ``run_build`` governs.
        tier: The test tier the plan resolved. None reads it from the Gate
              job's environment (:meth:`TierContext.from_env`).

    Returns:
        The verdict, whose reason is written to be read in a check summary.

    """
    build = build or {}
    tier = TierContext.from_env() if tier is None else tier

    # A failed or cancelled plan computed no gate, so every list below is empty
    # and would read as nothing-to-check. Only success passes.
    if plan != SUCCESS:
        return GateVerdict(
            ok=False,
            reason=(
                f"The plan job reported {plan or 'nothing'}, so no gate was "
                f"computed and the checks below it decided nothing. A run that "
                f"verified nothing cannot report success."
            ),
        )

    # No results at all means a renamed job or a missing `env:` block, which
    # would otherwise pass forever.
    missing_inputs = [
        name
        for name, results in (("quality and test", checks), ("build", build))
        if not results and (run_checks if name != "build" else run_build)
    ]
    if missing_inputs:
        return GateVerdict(
            ok=False,
            reason=(
                f"The gate required {' and '.join(missing_inputs)} and was given "
                f"no result for them. The job is wired wrong -- check its `env:` "
                f"block names every job it needs."
            ),
        )

    every = {**checks, **build}
    broken = sorted(name for name, result in every.items() if result in _FAILED)
    if broken:
        return GateVerdict(ok=False, reason=f"Did not pass: {', '.join(broken)}.")

    absent: list[str] = []
    if run_checks:
        absent += [n for n, result in checks.items() if result != SUCCESS]
    if run_build:
        absent += [n for n, result in build.items() if result != SUCCESS]
    if absent:
        return GateVerdict(
            ok=False,
            reason=(
                f"The gate required these on this event and they did not pass: "
                f"{', '.join(sorted(absent))}. A skipped check is not a "
                f"passing check."
            ),
        )

    # The release tail does not wait for this job, so this records a short
    # release and cannot stop one. The Test job exports no counts, so the line
    # names the tier only.
    if run_checks and tier.release_short_of_full:
        return GateVerdict(
            ok=False,
            reason=(
                f"This run released with tests at tier "
                f"{tier.tier or 'unknown'}, and test.full.required_for_release "
                f"owed full. The plan should have forced full, so the workflow "
                f"is wired wrong. See the Test job's annotation for what did "
                f"not run."
            ),
        )

    ran = sorted(n for n, result in every.items() if result == SUCCESS)
    if not run_checks and not run_build:
        return GateVerdict(
            ok=True,
            reason=(
                "The gate resolved run-checks=false and run-build=false, so the "
                "checks were skipped by the doctrine -- this commit ships "
                "nothing. Skipped deliberately, not missed."
            ),
        )
    tier_line = tier.describe() if run_checks else ""
    return GateVerdict(ok=True, reason=f"Ran and passed: {', '.join(ran)}.{tier_line}")
