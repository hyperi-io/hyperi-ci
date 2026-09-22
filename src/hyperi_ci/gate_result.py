# Project:   HyperI CI
# File:      src/hyperi_ci/gate_result.py
# Purpose:   Decide whether a run may report success given what actually ran
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Turn "what actually ran" into a status context branch protection can require.

A run whose every gate skipped still concludes ``success``, and GitHub counts a
SKIPPED required check as satisfied. So a branch-protection rule naming
``ci / Quality`` is satisfied by Quality not running, and the doctrine's
deliberate skip is indistinguishable from a gate that never fired (issue #177).

:mod:`hyperi_ci.gate_audit` reports that fleet-wide on a schedule, after the
fact. This is the same question asked inside the run, where it can still stop a
merge: given the gate the plan job computed, did the jobs it required actually
execute?

The doctrine itself is unchanged. A push that ships nothing SHOULD skip quality
and test, and this passes it -- while saying so, so the reason is on the record
rather than inferred from a green tick.
"""

from dataclasses import dataclass

# A job that never started. GitHub reports this for a gated job, and for every
# job downstream of one that failed.
SKIPPED = "skipped"
SUCCESS = "success"

# Results that mean the job ran and did not pass. `cancelled` counts: a
# cancelled required job has verified nothing.
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


def evaluate(
    *,
    run_checks: bool,
    run_build: bool,
    plan: str,
    checks: dict[str, str],
    build: dict[str, str] | None = None,
) -> GateVerdict:
    """Decide whether a run may report success.

    The two gates are separate and must stay separate. ``run_checks`` governs
    quality and test; ``run_build`` governs build, and is publish-only --
    compiling a commit nothing ships is the line the doctrine draws. A normal
    PR therefore has run-checks true and run-build false, and a build that
    skipped there is correct rather than missing.

    Args:
        run_checks: Whether the doctrine required quality and test here.
        run_build: Whether the doctrine required a build here.
        plan: Result of the plan job. A skipped plan decided nothing, so
              nothing downstream can be trusted.
        checks: Job name to result, for the jobs ``run_checks`` governs.
        build: Job name to result, for the jobs ``run_build`` governs.

    Returns:
        The verdict, whose reason is written to be read in a check summary.

    """
    build = build or {}

    # The plan job must have SUCCEEDED, not merely "not skipped". A plan that
    # failed or was cancelled computed no gate, so every job under it skipped
    # and every list below is empty -- which read as nothing-to-check and
    # passed. Anything other than success is a refusal.
    if plan != SUCCESS:
        return GateVerdict(
            ok=False,
            reason=(
                f"The plan job reported {plan or 'nothing'}, so no gate was "
                f"computed and the checks below it decided nothing. A run that "
                f"verified nothing cannot report success."
            ),
        )

    # A gate that asked for checks and was handed no results at all is a
    # misconfigured job, not a clean run -- a renamed job or a missing `env:`
    # block reaches here and would otherwise pass forever.
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
    return GateVerdict(ok=True, reason=f"Ran and passed: {', '.join(ran)}.")
