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
    def test_quality_deepens_history_for_secret_scan(self, workflow_name: str) -> None:
        # The quality checkout is depth-1; gitleaks scans the branch git log
        # for secrets and would only see HEAD without deepened history. Every
        # language workflow must deepen immediately before running quality.
        # (Conventional-commit validation moved to the commit-check job -- see
        # TestCommitCheckJob.)
        wf = _load_workflow(workflow_name)
        steps = wf["jobs"]["quality"]["steps"]
        names = [s.get("name") for s in steps]
        assert "Deepen history for secret scan" in names, (
            f"{workflow_name}: quality job missing the history-deepen step"
        )
        i = names.index("Deepen history for secret scan")
        assert names[i + 1] == "Run quality checks", (
            f"{workflow_name}: deepen step must run immediately before quality"
        )

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


class TestMainOnlyPublishGate:
    """Branch-mode decision 1 (docs/plans/2026-07-branch-mode): a push can
    only ever publish from main. The rule lives HERE, in the gate SSOT --
    not in downstream job `if:` conditions. A `Publish: true` trailer on a
    branch push is ignored LOUDLY (::warning::), never silently."""

    def _gate_step(self) -> dict:
        path = ACTIONS_DIR / "predict-version" / "action.yml"
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        return next(s for s in action["runs"]["steps"] if s.get("id") == "gate")

    def test_push_publish_requires_main_ref(self) -> None:
        run = str(self._gate_step()["run"])
        assert 'github.ref }}" != "refs/heads/main"' in run, (
            "gate must reject non-main refs on push before any trailer match"
        )
        # Ordering: the non-main guard must EXIT before the main trailer
        # match that can set will-publish=true. Substring presence alone
        # would stay green if a refactor moved the guard after the match.
        guard = run.index('!= "refs/heads/main"')
        main_trailer = run.index("push to main")
        assert guard < main_trailer, (
            "non-main guard must precede the main trailer publish decision"
        )
        # The guard block must exit 0 (validate-only), not fall through.
        assert "exit 0" in run[guard:main_trailer], (
            "non-main guard block must exit before the trailer decision"
        )

    def test_nonmain_trailer_warns_loudly(self) -> None:
        # No silent skips: a trailer on a branch must emit ::warning::.
        run = str(self._gate_step()["run"])
        nonmain_block_start = run.index('!= "refs/heads/main"')
        nonmain_block = run[nonmain_block_start:]
        assert "::warning::" in nonmain_block, (
            "ignored trailer on a non-main ref must warn loudly"
        )

    def test_dispatch_stays_explicit_publish(self) -> None:
        # workflow_dispatch remains an explicit publish trigger (a deliberate
        # act by someone with actions:write). It is checked FIRST, before the
        # ref guard -- rehearsal dispatches on fixture branches rely on this.
        run = str(self._gate_step()["run"])
        dispatch = run.index("workflow_dispatch")
        guard = run.index('!= "refs/heads/main"')
        assert dispatch < guard, "dispatch bypass must precede the non-main ref guard"


class TestFirstReleaseAndOrphanGuards:
    """issue #37 follow-up: tag-less repos declare their starting version
    via VERSION (shipped verbatim); orphaned-tag repos fail loud at plan
    time instead of predicting a taken/regressed version."""

    def _predict_step(self) -> dict:
        path = ACTIONS_DIR / "predict-version" / "action.yml"
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        return next(s for s in action["runs"]["steps"] if s.get("id") == "predict")

    def test_predict_fails_loud_on_orphaned_tags(self) -> None:
        # v* tags in refs but none reachable from HEAD = a past history
        # rewrite (the #37 damage signature). Must fail at plan time with
        # actionable guidance, not predict 1.0.0 and die later on the
        # tag collision.
        run = str(self._predict_step()["run"])
        assert "--merged HEAD" in run, (
            "predict step must check tag reachability (--merged HEAD)"
        )
        # Polarity: tags exist AND none reachable -> error out.
        assert '-n "$tags_all" && -z "$tags_reachable"' in run, (
            "orphan guard polarity: fire when tags exist but none reachable"
        )
        assert "recover-tags.py" in run and "publish --version" in run, (
            "orphan-guard error must name the escape hatches"
        )

    def test_predict_first_release_uses_version_file(self) -> None:
        # Tag-less repo: a committed VERSION file declares the starting
        # version verbatim; no VERSION keeps semantic-release's 1.0.0.
        run = str(self._predict_step()["run"])
        # Polarity: only on a genuinely tag-less repo AND with a VERSION file.
        assert '-z "$tags_all" && -f VERSION' in run, (
            "VERSION override must fire only on a tag-less repo with VERSION"
        )
        assert r"^[0-9]+\.[0-9]+\.[0-9]+$" in run, (
            "VERSION content must be validated as strict X.Y.Z before use"
        )

    def test_predict_early_collision_guard(self) -> None:
        # Plan-time twin of the _release-tail off-HEAD guard: a predicted
        # version whose tag already exists off-HEAD fails before the build.
        run = str(self._predict_step()["run"])
        assert "refs/tags/v${version}^{commit}" in run, (
            "predict step must check the predicted tag for an off-HEAD collision"
        )

    def test_predict_guard_ordering(self) -> None:
        # The guards and the VERSION override must all run BEFORE the
        # version is emitted to GITHUB_OUTPUT — substring presence alone
        # would stay green if a refactor moved a guard after the emit,
        # silently disarming it.
        run = str(self._predict_step()["run"])
        emit = run.index('echo "version=$version"')
        assert run.index("--merged HEAD") < emit, (
            "orphan guard must run before the version is emitted"
        )
        assert run.index('-z "$tags_all" && -f VERSION') < emit, (
            "VERSION override must apply before the version is emitted"
        )
        assert run.index("refs/tags/v${version}^{commit}") < emit, (
            "collision guard must run before the version is emitted"
        )
        # The override rewrites $version, so the collision guard must
        # check the FINAL value: override strictly before collision guard.
        assert run.index('-z "$tags_all" && -f VERSION') < run.index(
            "refs/tags/v${version}^{commit}"
        ), "collision guard must check the post-override version"

    def test_release_tail_first_release_uses_tag_head(self) -> None:
        # On a tag-less repo the real semantic-release run would tag its
        # own 1.0.0 default, diverging from the plan's resolved starting
        # version. The tail must materialise the plan's next-version via
        # tag-head instead — one version oracle.
        wf = _load_workflow("_release-tail.yml")
        steps = wf["jobs"]["tag-and-publish"]["steps"]
        sr = next(s for s in steps if s.get("name") == "Tag (semantic-release)")
        run = str(sr["run"])
        # Polarity: the -z (tag-less) branch runs tag-head; the else
        # branch runs semantic-release. Substring presence alone would
        # stay green with the branches swapped or the predicate inverted.
        assert "if [ -z \"$(git tag --list 'v[0-9]*')\" ]" in run, (
            "Tag step must branch on the tag-less (-z) predicate"
        )
        idx_if = run.index("if [ -z ")
        idx_tag_head = run.index("tag-head --bump ${{ inputs.next-version }}")
        idx_else = run.index("else")
        idx_sr = run.index("npx semantic-release")
        assert idx_if < idx_tag_head < idx_else < idx_sr, (
            "tag-less branch must be tag-head; the else branch must be "
            "the real semantic-release run"
        )
        # tag-head goes through `gh api` — the step needs GH_TOKEN.
        assert "GH_TOKEN" in sr.get("env", {}), (
            "Tag step must export GH_TOKEN for tag-head's gh api call"
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


class TestCommitCheckJob:
    """The commit-check job is the landing gate for conventional-commit
    messages: it validates what actually reaches main (push) and gives
    advisory feedback on PRs. Deliberately independent of the run-checks
    gate so a merge to main is validated even when it is not publish-worthy
    (that gate skips non-publish main pushes). See commit_validation.run +
    CLAUDE.md CI gate doctrine.
    """

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_commit_check_job_exists(self, workflow_name: str) -> None:
        wf = _load_workflow(workflow_name)
        assert "commit-check" in wf.get("jobs", {}), (
            f"{workflow_name}: missing the commit-check landing-gate job"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_commit_check_not_gated_on_run_checks(self, workflow_name: str) -> None:
        # The whole point: it must NOT depend on plan.outputs.run-checks (that
        # gate skips non-publish main pushes -- exactly where merge-to-main
        # enforcement is needed). It gates on the event instead, and stays
        # independent of plan so it runs in parallel.
        wf = _load_workflow(workflow_name)
        job = wf["jobs"]["commit-check"]
        ifc = str(job.get("if", ""))
        assert "run-checks" not in ifc, (
            f"{workflow_name}: commit-check must NOT gate on run-checks"
        )
        assert "refs/heads/main" in ifc and "pull_request" in ifc, (
            f"{workflow_name}: commit-check must run on push-to-main + PRs"
        )
        assert "needs" not in job, (
            f"{workflow_name}: commit-check must be independent of plan"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_commit_check_runs_check_commits(self, workflow_name: str) -> None:
        wf = _load_workflow(workflow_name)
        steps = wf["jobs"]["commit-check"]["steps"]
        # Full history so before..after / base..HEAD resolves without a
        # separate deepen step.
        checkout = next(s for s in steps if "checkout" in str(s.get("uses", "")))
        assert checkout.get("with", {}).get("fetch-depth") == 0, (
            f"{workflow_name}: commit-check checkout must be fetch-depth: 0"
        )
        runs = " ".join(str(s.get("run", "")) for s in steps)
        assert "check-commits" in runs, (
            f"{workflow_name}: commit-check must invoke `hyperi-ci check-commits`"
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
