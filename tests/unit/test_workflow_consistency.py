# Project:   HyperI CI
# File:      tests/unit/test_workflow_consistency.py
# Purpose:   Mechanical drift-prevention for cross-language workflow gates
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
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


class TestFromHeadThreading:
    """issue #35: from-head + bump inputs must thread through every layer —
    consumer ci.yml -> <lang>-ci.yml workflow_call -> predict-version (plan) ->
    _release-tail.yml -> Tag & Publish. Otherwise `hyperi-ci publish` dispatches
    inputs the CI silently ignores."""

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_workflow_call_accepts_from_head_and_bump(self, workflow_name: str) -> None:
        wf = _load_workflow(workflow_name)
        # PyYAML parses bare `on:` as the boolean True (YAML 1.1).
        on = wf.get("on") or wf.get(True, {})
        wc = on.get("workflow_call", {}).get("inputs", {})
        assert "from-head" in wc, f"{workflow_name}: workflow_call missing from-head"
        assert "bump" in wc, f"{workflow_name}: workflow_call missing bump"

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_workflow_call_accepts_submodules(self, workflow_name: str) -> None:
        # All four language workflows expose the optional `submodules`
        # input + init step for public submodules (issue #39). Parity so a
        # rust/go/ts repo with a public submodule works the same as python.
        wf = _load_workflow(workflow_name)
        on = wf.get("on") or wf.get(True, {})
        wc = on.get("workflow_call", {}).get("inputs", {})
        assert "submodules" in wc, f"{workflow_name}: workflow_call missing submodules"
        assert wc["submodules"].get("default", None) == "", (
            f"{workflow_name}: submodules must default to '' (no-op for "
            "consumers that don't set it)"
        )
        # The test job must actually init submodules when the input is set.
        steps = wf["jobs"]["test"]["steps"]
        init = [s for s in steps if s.get("name") == "Init submodules"]
        assert init, f"{workflow_name}: test job missing 'Init submodules' step"
        assert init[0].get("if") == "${{ inputs.submodules != '' }}", (
            f"{workflow_name}: Init submodules must gate on the input"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_dispatch_tag_is_optional(self, workflow_name: str) -> None:
        # from-head dispatch has no tag; tag must be optional (else `gh
        # workflow run` errors before the plan job even starts).
        wf = _load_workflow(workflow_name)
        on = wf.get("on") or wf.get(True, {})
        dispatch_inputs = on.get("workflow_dispatch", {}).get("inputs", {})
        assert dispatch_inputs.get("tag", {}).get("required") is not True, (
            f"{workflow_name}: workflow_dispatch.tag must be optional for "
            "from-head dispatch (issue #35)"
        )
        assert "from-head" in dispatch_inputs and "bump" in dispatch_inputs, (
            f"{workflow_name}: workflow_dispatch missing from-head/bump"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_predict_version_receives_from_head_and_bump(
        self, workflow_name: str
    ) -> None:
        wf = _load_workflow(workflow_name)
        plan = wf["jobs"]["plan"]["steps"]
        predict_step = next(
            s for s in plan if "predict-version" in str(s.get("uses", ""))
        )
        with_inputs = predict_step.get("with", {})
        assert "from-head" in with_inputs and "bump" in with_inputs, (
            f"{workflow_name}.plan: predict-version must receive from-head/bump "
            "so the version is resolved on a from-head dispatch (#35)"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_release_tail_receives_from_head_and_bump(self, workflow_name: str) -> None:
        wf = _load_workflow(workflow_name)
        tail_call = next(
            j
            for j in wf["jobs"].values()
            if "_release-tail.yml" in str(j.get("uses", ""))
        )
        with_inputs = tail_call.get("with", {})
        assert "from-head" in with_inputs and "bump" in with_inputs, (
            f"{workflow_name}: _release-tail call must forward from-head/bump"
        )

    def test_release_tail_accepts_from_head_and_bump(self) -> None:
        wf = _load_workflow("_release-tail.yml")
        on = wf.get("on") or wf.get(True, {})
        wc = on.get("workflow_call", {}).get("inputs", {})
        assert "from-head" in wc and "bump" in wc

    def test_release_tail_tags_head_on_dispatch_auto(self) -> None:
        # semantic-release tags HEAD on from-head + bump=auto (re-uses the
        # push tagger). Forced bumps use tag-head instead.
        steps = _load_workflow("_release-tail.yml")["jobs"]["tag-and-publish"]["steps"]
        sr = next(s for s in steps if s.get("name") == "Tag (semantic-release)")
        ifc = str(sr["if"])
        assert "from-head" in ifc and "bump" in ifc and "auto" in ifc, (
            "Tag (semantic-release) if: must extend to from-head + bump=auto"
        )

    def test_release_tail_has_forced_tag_step(self) -> None:
        steps = _load_workflow("_release-tail.yml")["jobs"]["tag-and-publish"]["steps"]
        forced = [s for s in steps if s.get("id") == "forcedtag"]
        assert forced, "missing forced-bump tag step (tag-head) for from-head"
        ifc = str(forced[0]["if"])
        # Fires only on from-head with a non-auto bump.
        assert "from-head" in ifc
        assert "bump != 'auto'" in ifc, (
            "forced-tag step must skip when bump == 'auto' (auto uses semantic-release)"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_build_stamps_on_from_head_dispatch(self, workflow_name: str) -> None:
        # The from-head build-stamp gap (issue #37 / #27): HEAD's committed tree
        # is stale under the tagger-only model, so a from-head release MUST
        # stamp the resolved next-version before building — else the published
        # binary introspects itself as the old version. The stamp step must
        # fire on push AND on a from-head dispatch.
        wf = _load_workflow(workflow_name)
        build_steps = wf["jobs"]["build"]["steps"]
        stamp = next(
            s for s in build_steps if s.get("name") == "Stamp predicted version"
        )
        ifc = str(stamp["if"])
        assert "inputs.from-head" in ifc, (
            f"{workflow_name}.build: 'Stamp predicted version' must also run on a "
            "from-head dispatch (issue #37) — else the released binary reports a "
            "stale version."
        )
        assert "github.event_name == 'push'" in ifc, (
            f"{workflow_name}.build: stamp must still run on push."
        )


ACTIONS_DIR = Path(__file__).parent.parent.parent / ".github" / "actions"


def test_predict_version_forced_step_handles_explicit_version() -> None:
    # The forced step resolves ANY non-auto bump — patch/minor OR an explicit
    # X.Y.Z (the --version override, issue #37) — so plan stamps exactly what
    # tag-head later tags. Gating only on patch/minor would leave an explicit
    # version unresolved and the build would fail on an empty next-version.
    path = ACTIONS_DIR / "predict-version" / "action.yml"
    action = yaml.safe_load(path.read_text(encoding="utf-8"))
    forced = next(s for s in action["runs"]["steps"] if s.get("id") == "forced")
    ifc = str(forced["if"])
    assert "bump != 'auto'" in ifc and "bump != ''" in ifc, (
        "forced step must fire for any non-auto/non-empty bump (incl. an "
        "explicit X.Y.Z), not only patch/minor"
    )
    # Body must recognise a bare semver and use it verbatim.
    assert r"[0-9]+\.[0-9]+\.[0-9]+" in str(forced["run"]), (
        "forced step body must match a bare X.Y.Z to use it verbatim"
    )


class TestReleaseTailDecoupling:
    """issue #33: a Container failure must not block the primary publish,
    and a library must not boot Buildx / touch GHCR at all."""

    def _tail(self) -> dict:
        return _load_workflow("_release-tail.yml")

    def test_tag_and_publish_decoupled_from_container(self) -> None:
        # `always()` ensures Tag & Publish runs even when Container fails
        # or is skipped — the crate/GH release is never lost to a
        # transient container hiccup.
        job = self._tail()["jobs"]["tag-and-publish"]
        assert "always()" in str(job["if"]), (
            "tag-and-publish must use always() so a failed/skipped Container "
            "job does not block the publish (issue #33)."
        )

    def test_container_resolves_before_docker(self) -> None:
        # The Docker-touching steps must gate on the resolve step's output
        # so a library never pulls buildkit / logs in to GHCR.
        steps = self._tail()["jobs"]["container"]["steps"]
        assert any(s.get("id") == "resolve" for s in steps), (
            "container job must have a 'resolve' step before Docker setup."
        )
        docker_steps = [
            s
            for s in steps
            if any(k in str(s.get("uses", "")) for k in ("buildx", "login-action"))
        ]
        assert docker_steps, "expected Docker login/buildx steps in container job"
        for s in docker_steps:
            assert "steps.resolve.outputs.build" in str(s.get("if", "")), (
                f"Docker step {s.get('name')!r} must gate on resolve output "
                "so libraries skip Buildx/GHCR (issue #33)."
            )


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
