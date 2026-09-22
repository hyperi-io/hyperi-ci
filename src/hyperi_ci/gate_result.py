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
    plan: str,
    required: dict[str, str],
) -> GateVerdict:
    """Decide whether a run may report success.

    Args:
        run_checks: The gate the plan job computed -- whether the doctrine
                    required quality and test on this event.
        plan: Result of the plan job. A skipped plan decided nothing, so
              nothing downstream can be trusted.
        required: Job name to result, for the jobs ``run_checks`` governs.

    Returns:
        The verdict, whose reason is written to be read in a check summary.

    """
    if plan == SKIPPED:
        return GateVerdict(
            ok=False,
            reason=(
                "The plan job did not run, so no gate was computed and every "
                "check below it skipped. A run that decided nothing cannot "
                "report success."
            ),
        )

    broken = sorted(name for name, result in required.items() if result in _FAILED)
    if broken:
        return GateVerdict(ok=False, reason=f"Did not pass: {', '.join(broken)}.")

    if not run_checks:
        return GateVerdict(
            ok=True,
            reason=(
                "The gate resolved run-checks=false, so quality and test were "
                "skipped by the doctrine -- this commit ships nothing. Skipped "
                "deliberately, not missed."
            ),
        )

    absent = sorted(name for name, result in required.items() if result == SKIPPED)
    if absent:
        return GateVerdict(
            ok=False,
            reason=(
                f"The gate required checks on this event and these did not "
                f"run: {', '.join(absent)}. A skipped check is not a passing "
                f"check."
            ),
        )

    return GateVerdict(
        ok=True, reason=f"Ran and passed: {', '.join(sorted(required))}."
    )
