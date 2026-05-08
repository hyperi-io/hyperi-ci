# Project:   HyperI CI
# File:      tests/unit/test_workflow_consistency.py
# Purpose:   Mechanical drift-prevention for cross-language workflow gates
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Workflow consistency tests.

Each `<lang>-ci.yml` file follows the contract in `docs/ARCHITECTURE.md`:
plan job first, downstream jobs gate on `plan.outputs.run-checks` (for
quality / test) or `plan.outputs.run-build` (for build).

Because the gate strings are duplicated across four files (deliberately
— see ARCHITECTURE.md "what's shared vs duplicated"), drift is the main
maintenance risk. This test catches drift mechanically: every gate
must match the canonical strings below.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).parent.parent.parent / ".github" / "workflows"
LANGUAGE_WORKFLOWS = ("rust-ci.yml", "python-ci.yml", "ts-ci.yml", "go-ci.yml")

# Canonical gate strings. If you need to change one of these, change it
# here AND in every language workflow in the same commit. Anything else
# is drift.
CHECKS_GATE = "needs.plan.outputs.run-checks == 'true'"
BUILD_GATE = "needs.plan.outputs.run-build == 'true'"

# Jobs that must exist in every language workflow with the listed gates.
EXPECTED_JOBS: dict[str, str] = {
    "quality": CHECKS_GATE,
    "test": CHECKS_GATE,
    "build": BUILD_GATE,
}


def _load_workflow(name: str) -> dict:
    path = WORKFLOW_DIR / name
    if not path.is_file():
        pytest.skip(f"{name} not present yet (mid-rollout)")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
def test_workflow_has_plan_job_first(workflow_name: str) -> None:
    """Every language workflow's first job must be `plan`."""
    wf = _load_workflow(workflow_name)
    jobs = wf.get("jobs", {})
    job_names = list(jobs.keys())
    assert job_names, f"{workflow_name}: no jobs defined"
    assert job_names[0] == "plan", (
        f"{workflow_name}: first job is '{job_names[0]}', expected 'plan'. "
        f"Plan must run first so downstream jobs can gate on its outputs."
    )


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
@pytest.mark.parametrize("job_name,expected_gate", list(EXPECTED_JOBS.items()))
def test_job_uses_canonical_gate(
    workflow_name: str, job_name: str, expected_gate: str
) -> None:
    """Each gated job's `if:` must match the canonical string exactly."""
    wf = _load_workflow(workflow_name)
    jobs = wf.get("jobs", {})
    if job_name not in jobs:
        pytest.fail(
            f"{workflow_name}: missing required job '{job_name}'. "
            f"Every language workflow must have plan/quality/test/build."
        )
    job = jobs[job_name]
    actual = job.get("if", "")
    # Allow extra conditions joined with `||` for some jobs (e.g. test
    # job may also need `|| github.event_name == 'workflow_dispatch'`),
    # but the canonical gate string MUST appear unmodified.
    assert expected_gate in actual, (
        f"{workflow_name}.{job_name}: `if:` does not contain canonical "
        f"gate string.\n  expected substring: {expected_gate}\n  "
        f"actual: {actual}"
    )


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
def test_plan_job_uses_predict_version_composite(workflow_name: str) -> None:
    """Plan job MUST call the predict-version composite action — not a
    re-implementation. This is the single source of truth for the gate
    decision logic.
    """
    wf = _load_workflow(workflow_name)
    plan = wf.get("jobs", {}).get("plan", {})
    steps = plan.get("steps", [])
    uses_predict = any("predict-version" in str(step.get("uses", "")) for step in steps)
    assert uses_predict, (
        f"{workflow_name}.plan: must call hyperi-io/hyperi-ci/.github/"
        f"actions/predict-version composite action. Re-implementing the "
        f"gate decision is forbidden — see docs/ARCHITECTURE.md."
    )


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
def test_release_tail_uses_shared_workflow(workflow_name: str) -> None:
    """Release tail MUST be the shared `_release-tail.yml` workflow.
    This is the second of two allowed indirection layers (per
    docs/ARCHITECTURE.md).
    """
    wf = _load_workflow(workflow_name)
    jobs = wf.get("jobs", {})
    # Look for any job that calls _release-tail.yml
    tail_calls = [
        job for job in jobs.values() if "_release-tail.yml" in str(job.get("uses", ""))
    ]
    assert tail_calls, (
        f"{workflow_name}: no job uses _release-tail.yml. "
        f"Every language workflow must delegate container + tag-and-publish "
        f"to the shared release-tail workflow."
    )
