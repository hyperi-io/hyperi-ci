# Project:   HyperI CI
# File:      tests/unit/test_workflow_consistency.py
# Purpose:   Mechanical drift-prevention for cross-language workflow gates
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Workflow consistency tests.

Each `<lang>-ci.yml` file follows the contract in `docs/architecture.md`:
plan job first, downstream jobs gate on `plan.outputs.run-checks` (for
quality / test) or `plan.outputs.run-build` (for build).

Because the gate strings are duplicated across four files (deliberately
- see docs/architecture.md "what's shared vs duplicated"), drift is the main
maintenance risk. This test catches drift mechanically: every gate
must match the canonical strings below.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
    """issue #35: from-head + bump inputs must thread through every layer --
    consumer ci.yml -> <lang>-ci.yml workflow_call -> predict-version (plan) ->
    _release-tail.yml -> Tag & Release. Otherwise `hyperi-ci release` dispatches
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
        # (Conventional-commit validation moved to the commit-check job - see
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

    def test_release_tail_blocks_the_tag_when_a_shipped_container_fails(self) -> None:
        # A container-shipping project must not cut a tag/release its image
        # does not back (issue #102): dfe-loader v1.18.19 advertised a version
        # GHCR never had. A library keeps the issue-#33 decoupling.
        wf = _load_workflow("_release-tail.yml")
        container = wf["jobs"]["container"]
        assert container.get("outputs", {}).get("ships-container"), (
            "container job must expose ships-container so tag-and-release can "
            "tell a failed deliverable from a project that ships no container"
        )
        ifc = " ".join(str(wf["jobs"]["tag-and-release"]["if"]).split())
        assert "needs.container.result == 'success'" in ifc, (
            "tag-and-release must require a successful container..."
        )
        assert "needs.container.outputs.ships-container != 'true'" in ifc, (
            "...unless the project ships no container (issue #33 decoupling)"
        )

    def test_release_tail_tags_head_on_dispatch_auto(self) -> None:
        # semantic-release tags HEAD on from-head + bump=auto (re-uses the
        # push tagger). Forced bumps use tag-head instead.
        steps = _load_workflow("_release-tail.yml")["jobs"]["tag-and-release"]["steps"]
        sr = next(s for s in steps if s.get("name") == "Tag (semantic-release)")
        ifc = str(sr["if"])
        assert "from-head" in ifc and "bump" in ifc and "auto" in ifc, (
            "Tag (semantic-release) if: must extend to from-head + bump=auto"
        )

    def test_release_tail_installs_uv_before_anything_can_fail(self) -> None:
        # The failure notice runs `uvx`; with uv installed after checkout, a
        # checkout failure left the notice with `command not found`.
        steps = _load_workflow("_release-tail.yml")["jobs"]["tag-and-release"]["steps"]
        assert steps[0].get("name") == "Install uv", (
            "_release-tail.tag-and-release: 'Install uv' must be the first step so "
            "the failure notice can run whatever fails after it"
        )
        names = [s.get("name") for s in steps]
        assert names.count("Install uv") == 1, "one uv install per job"

    def test_release_tail_has_forced_tag_step(self) -> None:
        steps = _load_workflow("_release-tail.yml")["jobs"]["tag-and-release"]["steps"]
        forced = [s for s in steps if s.get("id") == "forcedtag"]
        assert forced, "missing forced-bump tag step (tag-head) for from-head"
        ifc = str(forced[0]["if"])
        # Fires only on from-head with a non-auto bump.
        assert "from-head" in ifc
        assert "bump != 'auto'" in ifc, (
            "forced-tag step must skip when bump == 'auto' (auto uses semantic-release)"
        )

    def test_release_tail_restamps_before_committing_artefacts(self) -> None:
        """VERSION must be stamped in the job that commits it.

        release-commit lists VERSION in RELEASE_ARTEFACTS and reads it off
        disk, but tag-and-release checks out the TAG, whose VERSION is the
        pre-release value. The build's stamp runs on another runner and only
        dist/ + ci-tmp/ are passed between them, so without a stamp here the
        uploaded blob matches the branch, the tree is unchanged for that path,
        and VERSION never moves in any repo on this pipeline.
        """
        steps = _load_workflow("_release-tail.yml")["jobs"]["tag-and-release"]["steps"]
        names = [s.get("name") for s in steps]

        assert "Stamp the released version" in names, (
            "_release-tail.tag-and-release: no stamp step before release-commit, "
            "so VERSION can never move"
        )
        stamp_at = names.index("Stamp the released version")
        commit_at = names.index("Commit rendered release artefacts")
        assert stamp_at < commit_at, (
            "the stamp must precede release-commit, which reads VERSION off disk"
        )

        stamp = steps[stamp_at]
        commit = steps[commit_at]
        # Same gate as the commit it feeds: stamping a release that did not
        # publish would leave a version on disk nothing shipped.
        assert str(stamp["if"]) == str(commit["if"]), (
            "stamp and release-commit must share a condition"
        )
        assert stamp["continue-on-error"] is True, (
            "bookkeeping after a shipped release must not turn it red"
        )
        # The version arrives by env so it is never read as shell.
        assert "RELEASE_VERSION" in stamp["env"]
        assert stamp["env"]["RELEASE_VERSION"] == commit["run"].split('"')[1], (
            "stamp and release-commit must stamp and commit the SAME version"
        )
        # A repo with no VERSION file has opted out; stamp_version would create
        # one, and release-commit would then commit a file it never had.
        assert "-f VERSION" in stamp["run"], (
            "stamp step must not create a VERSION file where none exists"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_build_stamps_on_from_head_dispatch(self, workflow_name: str) -> None:
        # The from-head build-stamp gap (issue #37 / #27): HEAD's committed tree
        # is stale under the tagger-only model, so a from-head release MUST
        # stamp the resolved next-version before building -- else the published
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

    def test_container_stamps_predicted_version_before_the_build(self) -> None:
        # The container job checks out HEAD on its own runner, so the Build job's
        # stamp never reaches the tree the Dockerfile copies (dfe-engine#271).
        steps = _load_workflow("_release-tail.yml")["jobs"]["container"]["steps"]
        names = [s.get("name") for s in steps]
        assert "Stamp predicted version" in names, (
            "_release-tail.container: no 'Stamp predicted version' step -- the "
            "image is built from the committed placeholder version."
        )
        assert names.index("Stamp predicted version") < names.index(
            "Build container"
        ), "_release-tail.container: the stamp must run before 'Build container'."
        stamp = steps[names.index("Stamp predicted version")]
        ifc = str(stamp["if"])
        assert "steps.resolve.outputs.build" in ifc, (
            "container stamp must gate on the resolve step, like every Docker step"
        )
        assert "inputs.will-publish == 'true'" in ifc, (
            "container stamp must only rewrite the tree on a publish run"
        )
        assert "inputs.tag == ''" in ifc, (
            "a tag dispatch checks out a tree that already carries its version"
        )
        assert "stamp-version" in str(stamp["run"]), (
            "container stamp must use the shared `hyperi-ci stamp-version` command"
        )


ACTIONS_DIR = Path(__file__).parent.parent.parent / ".github" / "actions"


def test_predict_version_forced_step_handles_explicit_version() -> None:
    # The forced step resolves ANY non-auto bump -- patch/minor OR an explicit
    # X.Y.Z (the --version override, issue #37) -- so plan stamps exactly what
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
    """Branch-mode decision 1 (docs/plans/2026-07-branch-mode) as amended by
    issue #144: a push publishes from main, or from a branch the release
    config declares `prerelease` - which spends no stable version. Every
    other ref validates only. The rule lives HERE, in the gate SSOT - not in
    downstream job `if:` conditions. A `Publish: true` trailer on any other
    branch push is ignored LOUDLY (::warning::), never silently."""

    # The comment landmark that opens the trailer decision. Named once so a
    # reword updates one line rather than four ordering assertions.
    TRAILER_DECISION = "push to a release branch"

    def _gate_step(self) -> dict:
        path = ACTIONS_DIR / "predict-version" / "action.yml"
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        return next(s for s in action["runs"]["steps"] if s.get("id") == "gate")

    def test_push_publish_requires_a_release_ref(self) -> None:
        run = str(self._gate_step()["run"])
        assert 'github.ref }}" != "refs/heads/main"' in run, (
            "gate must test the ref on push before any trailer match"
        )
        # Ordering: the ref guard must EXIT before the trailer match that can
        # set will-publish=true. Substring presence alone would stay green if
        # a refactor moved the guard after the match.
        guard = run.index('!= "refs/heads/main"')
        trailer = run.index(self.TRAILER_DECISION)
        assert guard < trailer, "ref guard must precede the trailer publish decision"
        # The guard block must exit 0 (validate-only), not fall through.
        assert "exit 0" in run[guard:trailer], (
            "ref guard block must exit before the trailer decision"
        )

    def test_a_declared_prerelease_branch_reaches_the_trailer_decision(self) -> None:
        # issue #144: the whole point. A non-main ref that the release config
        # declares prerelease must fall THROUGH the guard, not exit in it.
        run = str(self._gate_step()["run"])
        guard = run.index('!= "refs/heads/main"')
        trailer = run.index(self.TRAILER_DECISION)
        block = run[guard:trailer]
        assert "prerelease_branch.py" in block, (
            "the ref guard must ask the prerelease-branch helper, not "
            "hard-code the branch set"
        )
        assert '"$prerelease" != "true"' in block, (
            "only a ref that is NOT a declared prerelease branch may exit the "
            "guard as validate-only"
        )

    def test_the_prerelease_helper_ships_beside_the_action(self) -> None:
        # The composite loads it by path on a runner with no hyperi-ci
        # installed, so it must live in the action directory.
        helper = ACTIONS_DIR / "predict-version" / "prerelease_branch.py"
        assert helper.is_file(), f"{helper} must exist for the gate to call"

    def test_ignored_trailer_warns_loudly(self) -> None:
        # No silent skips: a trailer on a non-release branch must emit
        # ::warning::.
        run = str(self._gate_step()["run"])
        guard = run.index('!= "refs/heads/main"')
        assert "::warning::" in run[guard:], (
            "ignored trailer on a non-release ref must warn loudly"
        )

    def test_dispatch_is_resolved_before_the_ref_guard(self) -> None:
        # A dispatch is resolved FIRST, before the ref guard - rehearsal
        # dispatches on fixture branches rely on this.
        run = str(self._gate_step()["run"])
        dispatch = run.index("workflow_dispatch")
        guard = run.index('!= "refs/heads/main"')
        assert dispatch < guard, "dispatch bypass must precede the non-main ref guard"

    def test_bare_dispatch_is_validate_only_not_a_refusal(self) -> None:
        # will-publish=false is what keeps a versionless dispatch off the
        # registry (issue #105), and it leaves an on-demand run that publishes
        # nothing expressible (issue #111) where a refusal did not.
        run = str(self._gate_step()["run"])
        block = run[run.index("workflow_dispatch") : run.index(self.TRAILER_DECISION)]
        assert "will-publish=false" in block, (
            "a bare dispatch must resolve validate-only"
        )
        assert "exit 1" not in block, "a bare dispatch must not fail the run"

    def test_bare_dispatch_warns_loudly(self) -> None:
        # No silent no-ops: someone who meant to release must be told that
        # nothing was published.
        run = str(self._gate_step()["run"])
        block = run[run.index("workflow_dispatch") : run.index(self.TRAILER_DECISION)]
        assert "::warning::" in block, (
            "a validate-only dispatch must say nothing was published"
        )

    def test_validate_only_dispatch_still_runs_the_gates(self) -> None:
        # A verification run that compiles nothing verifies nothing, and a
        # skipped gate reads as a pass (issue #96), so run-checks and run-build
        # must both cover a dispatch that is not publishing.
        path = ACTIONS_DIR / "predict-version" / "action.yml"
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        derive = next(s for s in action["runs"]["steps"] if s.get("id") == "derive")
        run = str(derive["run"])
        split = run.index("run_build=false")
        assert '"$event_name" == "workflow_dispatch"' in run[:split], (
            "derive must set run_build for a validate-only dispatch"
        )
        assert '"$event_name" == "workflow_dispatch"' in run[split:], (
            "derive must set run_checks for a validate-only dispatch"
        )


class TestReleaseWorthyGate:
    """issue #124: the doctrine runs checks on PR review and RELEASE-WORTHY
    pushes, but the code only ever asked for the `Publish: true` trailer --
    which marks a push that PUBLISHES, a strictly smaller set. An ordinary
    squash merge of a `fix:` to main ran no quality, no test, and went green."""

    def _step(self, step_id: str) -> dict:
        path = ACTIONS_DIR / "predict-version" / "action.yml"
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        return next(s for s in action["runs"]["steps"] if s.get("id") == step_id)

    def _derive_halves(self) -> tuple[str, str]:
        """The run_build block and everything after it (the run_checks half)."""
        run = str(self._step("derive")["run"])
        start = run.index('if [[ "$will_publish" == "true" ]]; then')
        split = run.index("run_build=false")
        return run[start:split], run[split:]

    def test_run_checks_covers_a_release_worthy_push(self) -> None:
        _build, checks = self._derive_halves()
        assert '"$release_worthy" == "true"' in checks, (
            "derive must set run_checks for a release-worthy push to main"
        )

    def test_run_build_ignores_release_worthiness(self) -> None:
        # The red line in the doctrine: a commit that ships nothing must
        # still compile nothing. Only run_checks widens.
        build, _checks = self._derive_halves()
        assert "release_worthy" not in build, (
            "run_build must NOT key off release-worthiness -- a non-publishing "
            "commit compiles nothing (CI gate doctrine)"
        )

    def test_the_probe_only_runs_on_a_push_to_main(self) -> None:
        # A PR already runs the checks, and a feature-branch push must keep
        # the chore-skip fast path.
        ifc = str(self._step("worthy")["if"])
        assert "github.event_name == 'push'" in ifc, (
            "the worthiness probe must be gated to push events"
        )
        assert "refs/heads/main" in ifc, "the worthiness probe must be gated to main"

    def test_the_probe_does_not_resolve_its_own_range(self) -> None:
        # origin/main..HEAD is EMPTY right after a push to main (issue #52),
        # so a second range resolver here would validate nothing. One resolver,
        # in commit_range, reached through the shipped helper.
        run = str(self._step("worthy")["run"])
        assert "release_worthy.py" in run, "the probe must call the shipped helper"
        assert "git log" not in run, (
            "the probe must not derive a range in shell -- commit_range owns it"
        )

    def test_the_probe_ships_with_the_action(self) -> None:
        # The composite loads it by path out of its own checkout; a rename
        # would leave the step calling a file that is not there.
        path = (
            Path(__file__).resolve().parents[2]
            / ".github/actions/predict-version/release_worthy.py"
        )
        assert path.is_file(), f"{path} is referenced by action.yml but missing"

    def test_a_skipped_gate_on_main_warns_rather_than_notices(self) -> None:
        # A ::notice:: saying nothing ran reads as a pass (issue #96).
        run = str(self._step("derive")["run"])
        assert "::warning::" in run, (
            "derive must warn when run-checks is false on a push to main"
        )


class TestUnreleasedWorkIsSaidOutLoud:
    """A validate-only push to main reported success over a release backlog.

    scalo-rs sat 13 releasable commits and 26 days behind crates.io, a
    security floor bump among them, while every run went green. The gate
    warned about the strictly less consequential case three lines up -- a
    trailer on the wrong branch -- and echoed a plain line for this one.
    """

    # Where the no-trailer half of the gate begins.
    TRAILER_DETECTED = "Release trailer detected"

    def _gate_run(self) -> str:
        path = ACTIONS_DIR / "predict-version" / "action.yml"
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        gate = next(s for s in action["runs"]["steps"] if s.get("id") == "gate")
        return str(gate["run"])

    def _validate_only_half(self) -> str:
        run = self._gate_run()
        return run[run.index(self.TRAILER_DETECTED) :]

    def test_the_helper_ships_with_the_action(self) -> None:
        # The composite loads it by path out of its own checkout; a rename
        # would leave the step calling a file that is not there.
        helper = ACTIONS_DIR / "predict-version" / "unreleased.py"
        assert helper.is_file(), f"{helper} is referenced by action.yml but missing"

    def test_the_validate_only_branch_asks_what_is_waiting(self) -> None:
        assert "unreleased.py" in self._validate_only_half(), (
            "a validate-only push to a release branch must report what it left "
            "unreleased, not only that it published nothing"
        )

    def test_a_publishing_run_does_not_ask(self) -> None:
        # A run that IS shipping has nothing waiting by definition, and the
        # question costs two git calls.
        run = self._gate_run()
        assert "unreleased.py" not in run[: run.index(self.TRAILER_DETECTED)], (
            "the unreleased-work question belongs on the no-trailer path only"
        )

    def test_waiting_work_warns_rather_than_echoes(self) -> None:
        # The defect exactly: the same plain echo for "nothing to ship" and
        # for thirteen fixes waiting.
        assert "::warning::$unreleased" in self._validate_only_half(), (
            "unreleased work must be raised as ::warning::, not echoed into a "
            "folded log group"
        )

    def test_nothing_waiting_raises_nothing(self) -> None:
        # The third outcome stays quiet. A warning on every push is a warning
        # nobody reads, which is how the last three doc checks died.
        half = self._validate_only_half()
        assert '[[ -n "$unreleased" ]]' in half, (
            "the warning must be conditional on the helper finding work"
        )

    def test_the_gate_does_not_restate_the_releasable_types(self) -> None:
        # release_rules.py is the bump SSoT, and it honours a repo
        # .releaserc.json override that a shell copy never would.
        half = self._validate_only_half()
        for commit_type in ("feat", "perf", "fix:"):
            assert commit_type not in half, (
                f"the gate names '{commit_type}' -- ask release_rules through "
                "the helper instead of copying the type list into shell"
            )


class TestBuildChannelIsNotProxied:
    """The Tier 2 (PGO + BOLT) switch keys off the publish decision itself.

    `publish-target` was a proxy for it until v2.1.4 hollowed that input out
    (it is documented in the same file as 'legacy field, ignored'), leaving
    the workflow contradicting itself."""

    def test_no_workflow_level_channel_in_rust_ci(self) -> None:
        # A workflow-level default would apply to every job and put the proxy
        # back without anyone noticing.
        wf = _load_workflow("rust-ci.yml")
        assert "HYPERCI_CHANNEL" not in wf.get("env", {}), (
            "rust-ci.yml must not set HYPERCI_CHANNEL at workflow level -- the "
            "build step owns it, keyed off will-publish"
        )

    def test_the_build_step_keys_the_channel_off_will_publish(self) -> None:
        wf = _load_workflow("rust-ci.yml")
        build = next(
            s for s in wf["jobs"]["build"]["steps"] if s.get("name") == "Run build"
        )
        channel = str(build.get("env", {}).get("HYPERCI_CHANNEL", ""))
        assert "needs.plan.outputs.will-release" in channel, (
            "the build channel must follow the release decision, not a legacy "
            "publish-target proxy"
        )
        assert "publish-target" not in channel, (
            "publish-target is a legacy no-op -- it must not gate Tier 2"
        )

    def test_the_build_step_carries_version_identity_separately(self) -> None:
        # issue #144: tier and identity are independent, so the build needs
        # both. Deriving identity from HYPERCI_CHANNEL would make a fast
        # prerelease unreachable -- a release branch asks for the release tier.
        wf = _load_workflow("rust-ci.yml")
        build = next(
            s for s in wf["jobs"]["build"]["steps"] if s.get("name") == "Run build"
        )
        prerelease = str(build.get("env", {}).get("HYPERCI_PRERELEASE", ""))
        assert "needs.plan.outputs.prerelease" in prerelease, (
            "the build must read version identity from the plan job's prerelease output"
        )
        assert wf["jobs"]["plan"]["outputs"].get("prerelease"), (
            "the plan job must publish the prerelease output the build reads"
        )

    def test_the_release_tail_declares_no_channel(self) -> None:
        # Dead there: the tail runs only `run container` and `run release`,
        # and HYPERCI_CHANNEL is read in exactly one place -- the Rust BUILD
        # stage, which the tail never runs.
        wf = _load_workflow("_release-tail.yml")
        assert "HYPERCI_CHANNEL" not in wf.get("env", {}), (
            "_release-tail.yml sets HYPERCI_CHANNEL but runs no build stage, "
            "so nothing reads it"
        )


class TestBranchModeThreading:
    """Branch-mode decision 2 (docs/plans/2026-07-branch-mode): an opted-in
    pull_request runs build + container. The opt-in threads consumer ci.yml
    -> <lang>-ci.yml (branch-build input, HYPERCI_BRANCH_BUILD var fallback)
    -> predict-version derive (run-build) -> _release-tail container job.
    Arch breadth stays publish-only. The GA publish gate is untouched."""

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_workflow_call_accepts_branch_build(self, workflow_name: str) -> None:
        wf = _load_workflow(workflow_name)
        on = wf.get("on") or wf.get(True, {})
        wc = on.get("workflow_call", {}).get("inputs", {})
        assert "branch-build" in wc, (
            f"{workflow_name}: workflow_call missing branch-build"
        )
        assert wc["branch-build"].get("default", None) == "", (
            f"{workflow_name}: branch-build must default to '' (opt-in)"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_predict_version_receives_branch_build(self, workflow_name: str) -> None:
        wf = _load_workflow(workflow_name)
        plan = wf["jobs"]["plan"]["steps"]
        predict_step = next(
            s for s in plan if "predict-version" in str(s.get("uses", ""))
        )
        got = str(predict_step.get("with", {}).get("branch-build", ""))
        assert "inputs.branch-build" in got and "HYPERCI_BRANCH_BUILD" in got, (
            f"{workflow_name}.plan: predict-version must receive branch-build "
            "with the HYPERCI_BRANCH_BUILD var fallback"
        )

    def test_composite_derives_run_build_from_branch_build(self) -> None:
        path = ACTIONS_DIR / "predict-version" / "action.yml"
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "branch-build" in action.get("inputs", {}), (
            "predict-version composite missing the branch-build input"
        )
        derive = next(s for s in action["runs"]["steps"] if s.get("id") == "derive")
        run = str(derive["run"])
        # Polarity: pull_request AND branch_build=true -> run_build=true.
        assert '"$event_name" == "pull_request" && "$branch_build" == "true"' in run, (
            "derive must set run_build for opted-in pull_request events"
        )

    def test_go_matrix_arch_breadth_keys_off_will_publish(self) -> None:
        # Go still pays arm64 only on a shipping run. Rust moved to run-build
        # in issue #249 (see TestArm64Parity); Go did not, so the original
        # guard stays here rather than being deleted with it.
        wf = _load_workflow("go-ci.yml")
        plan = wf["jobs"]["plan"]["steps"]
        matrix = next(s for s in plan if s.get("id") == "matrix")
        run = str(matrix["run"])
        assert "steps.predict.outputs.will-release" in run, (
            "go-ci.yml: matrix arch breadth must key off will-release"
        )
        assert "steps.predict.outputs.run-build" not in run, (
            "go-ci.yml: matrix must NOT key off run-build (PR builds would go "
            "multi-arch)"
        )

    def test_release_tail_container_accepts_pull_request(self) -> None:
        wf = _load_workflow("_release-tail.yml")
        ifc = str(wf["jobs"]["container"]["if"])
        assert "github.event_name == 'pull_request'" in ifc, (
            "_release-tail container job must accept pull_request events "
            "(branch-mode dev builds)"
        )
        assert "fork != true" in ifc, (
            "_release-tail container job must keep the fork guard"
        )

    def test_release_tail_publish_still_gated_on_will_publish(self) -> None:
        # The GA publish gate is untouched by branch-mode: tag-and-release
        # fires ONLY on will-publish, never for a PR dev build.
        wf = _load_workflow("_release-tail.yml")
        ifc = str(wf["jobs"]["tag-and-release"]["if"])
        assert "inputs.will-publish == 'true'" in ifc, (
            "tag-and-release must stay gated on will-publish"
        )


_GH_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")

# The derive step's inputs, by the expression each is assigned from. Named
# here so a reworded expression fails the render rather than silently leaving
# a literal `${{ ... }}` in the script under test.
_DERIVE_INPUTS = {
    "${{ steps.gate.outputs.will-publish }}": "will_publish",
    "${{ github.event_name }}": "event_name",
    "${{ inputs.branch-build }}": "branch_build",
    "${{ github.ref }}": "git_ref",
    "${{ steps.worthy.outputs.release-worthy }}": "release_worthy",
    "${{ steps.predict.outputs.version || steps.forced.outputs.version }}": "version",
}

_MATRIX_INPUTS = {
    "${{ steps.predict.outputs.run-build }}": "run-build",
    "${{ steps.predict.outputs.run-arm64-check }}": "run-arm64-check",
}


def _composite_step(step_id: str) -> dict:
    path = ACTIONS_DIR / "predict-version" / "action.yml"
    action = yaml.safe_load(path.read_text(encoding="utf-8"))
    return next(s for s in action["runs"]["steps"] if s.get("id") == step_id)


def _run_step(script: str, *, cwd: Path, env: dict[str, str]) -> dict[str, str]:
    """Run a rendered step and return what it wrote to GITHUB_OUTPUT."""
    output = cwd / "github-output"
    output.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, **env, "GITHUB_OUTPUT": str(output)},
    )
    assert result.returncode == 0, (
        f"rendered step exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    written: dict[str, str] = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        written[key] = value
    return written


def _render(script: str, substitutions: dict[str, str], *, leftover: str = "") -> str:
    """Substitute GitHub expressions out of a step body."""
    for expression, value in substitutions.items():
        assert expression in script, f"expression gone from the step: {expression}"
        script = script.replace(expression, value)
    if leftover:
        script = _GH_EXPRESSION.sub(leftover, script)
    assert "${{" not in script, (
        f"an unrendered GitHub expression is left in the script under test:\n{script}"
    )
    return script


def _gate_outputs(expression: str) -> set[str]:
    """The plan outputs whose ``'true'`` makes a job's ``if:`` fire.

    Parsing rather than matching substrings: a clause this cannot read fails
    the test, so a gate rewritten into a shape the model does not cover can
    never pass by looking familiar.
    """
    names = set()
    for clause in str(expression).split("||"):
        match = re.fullmatch(
            r"needs\.plan\.outputs\.([a-z0-9-]+) == 'true'", clause.strip()
        )
        assert match, f"gate clause not in the `outputs.X == 'true'` shape: {clause!r}"
        names.add(match.group(1))
    return names


class TestArm64Parity:
    """issue #249: arm64 compiled only on a run that was already publishing.

    So an arm64-only defect first executed during the release meant to ship it,
    and the fix could not be exercised except by publishing again. A BOLT
    refusal over Cortex-A53 veneers reached dfe-receiver exactly that way.

    The tests below RENDER the shipped shell and run it, rather than asserting
    on substrings, because the question is what the matrix actually contains.
    """

    # will-publish, event, ref, release-worthy -- the four cases, in the terms
    # the derive step reads them.
    CASES = {
        "publish run": ("true", "push", "refs/heads/main", "true"),
        "validate-only dispatch": ("false", "workflow_dispatch", "refs/heads/main", ""),
        "release-worthy merge to main": ("false", "push", "refs/heads/main", "true"),
        "non-bumping merge to main": ("false", "push", "refs/heads/main", "false"),
    }

    @staticmethod
    def _rust_project(tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "Cargo.toml").write_text(
            '[package]\nname = "thing"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        return root

    def _derive(self, case: str, tmp_path: Path) -> dict[str, str]:
        """Run the composite's derive step for one case."""
        if not shutil.which("bash") or not shutil.which("python3"):
            pytest.skip("rendering the step needs bash + python3")
        will_publish, event, ref, worthy = self.CASES[case]
        script = _render(
            str(_composite_step("derive")["run"]),
            {
                expression: {
                    "will_publish": will_publish,
                    "event_name": event,
                    "branch_build": "",
                    "git_ref": ref,
                    "release_worthy": worthy,
                    "version": "1.2.3",
                }[name]
                for expression, name in _DERIVE_INPUTS.items()
            },
        )
        root = self._rust_project(tmp_path)
        return _run_step(
            script,
            cwd=root,
            env={
                "GITHUB_ACTION_PATH": str(ACTIONS_DIR / "predict-version"),
                "GITHUB_WORKSPACE": str(root),
            },
        )

    def _matrix(self, gates: dict[str, str], tmp_path: Path) -> list[str]:
        """Run rust-ci.yml's matrix step and return the os_arch legs it emits."""
        plan = _load_workflow("rust-ci.yml")["jobs"]["plan"]["steps"]
        step = next(s for s in plan if s.get("id") == "matrix")
        script = _render(
            str(step["run"]),
            {expression: gates[name] for expression, name in _MATRIX_INPUTS.items()},
            leftover="a-runner",
        )
        root = tmp_path / "matrix"
        root.mkdir()
        written = _run_step(script, cwd=root, env={})
        return [leg["os_arch"] for leg in json.loads(written["matrix"])["include"]]

    def _build_runs(self, gates: dict[str, str]) -> bool:
        condition = _load_workflow("rust-ci.yml")["jobs"]["build"]["if"]
        firing = _gate_outputs(condition)
        return any(gates.get(name) == "true" for name in firing)

    @pytest.mark.parametrize(
        "case,expected",
        [
            ("publish run", ["linux-amd64", "linux-arm64"]),
            ("validate-only dispatch", ["linux-amd64", "linux-arm64"]),
            ("release-worthy merge to main", ["linux-arm64"]),
        ],
    )
    def test_the_matrix_a_case_actually_builds(
        self, case: str, expected: list[str], tmp_path: Path
    ) -> None:
        gates = self._derive(case, tmp_path)
        assert self._build_runs(gates), f"{case}: the Build job did not fire"
        assert self._matrix(gates, tmp_path) == expected, (
            f"{case}: wrong arches for gates {gates}"
        )

    def test_a_non_bumping_merge_runs_no_build_at_all(self, tmp_path: Path) -> None:
        # The doctrine's red line: a commit that ships nothing compiles
        # nothing. Widening run-build here is what this change must NOT do.
        gates = self._derive("non-bumping merge to main", tmp_path)
        assert gates["run-build"] == "false"
        assert gates["run-arm64-check"] == "false"
        assert not self._build_runs(gates), (
            "a non-bumping merge to main reached the Build job -- that is the "
            "gate doctrine's red line, not a widening"
        )

    def test_run_build_still_ignores_release_worthiness(self, tmp_path: Path) -> None:
        # The parity build must arrive through run-arm64-check alone. If
        # run-build itself widened, the merge would build every arch and run
        # the release tail behind it.
        gates = self._derive("release-worthy merge to main", tmp_path)
        assert gates["run-build"] == "false", (
            "run-build widened to cover a release-worthy merge -- that builds "
            "amd64 and the container too, which this change does not buy"
        )
        assert gates["run-arm64-check"] == "true"

    def test_a_dispatch_owes_no_parity_check(self, tmp_path: Path) -> None:
        # run-build already covers a dispatch, so the parity signal stays off
        # and the matrix comes from run-build alone.
        gates = self._derive("validate-only dispatch", tmp_path)
        assert gates["run-build"] == "true"
        assert gates["run-arm64-check"] == "false"

    def test_the_helper_ships_with_the_action(self) -> None:
        # The composite loads it by path out of its own checkout; a rename
        # would leave the step calling a file that is not there.
        helper = ACTIONS_DIR / "predict-version" / "arm64_check.py"
        assert helper.is_file(), f"{helper} is referenced by action.yml but missing"

    def test_the_composite_publishes_the_output(self) -> None:
        path = ACTIONS_DIR / "predict-version" / "action.yml"
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        value = str(action["outputs"]["run-arm64-check"]["value"])
        assert "steps.derive.outputs.run-arm64-check" in value

    def test_only_rust_widens_its_build_gate(self) -> None:
        # python/ts/go have no arm64 leg to run, so the parity signal must not
        # reach their build gates -- it would build everything they have.
        assert "run-arm64-check" in str(
            _load_workflow("rust-ci.yml")["jobs"]["build"]["if"]
        )
        for name in ("python-ci.yml", "go-ci.yml", "ts-ci.yml", "ci.yml"):
            condition = str(_load_workflow(name)["jobs"]["build"].get("if", ""))
            assert "run-arm64-check" not in condition, (
                f"{name}: build gate reads run-arm64-check, which only rust-ci.yml "
                "has a matrix leg for"
            )

    def test_the_release_tail_skips_a_parity_build(self) -> None:
        # The tail used to be reached exactly when Build ran. A parity build
        # ships nothing, so a container build behind it is cost with no
        # deliverable.
        job = _load_workflow("rust-ci.yml")["jobs"]["release-tail"]
        assert _gate_outputs(job["if"]) == {"run-build"}, (
            "rust-ci.yml release-tail must gate on run-build alone, else the "
            "arm64-parity build drags a container build behind it"
        )


class TestBuildxCgroupParent:
    """issue #284: the buildx builder can be placed under the runner pod's cgroup."""

    def _buildx_step(self) -> dict:
        steps = _load_workflow("_release-tail.yml")["jobs"]["container"]["steps"]
        return next(s for s in steps if "setup-buildx-action" in str(s.get("uses", "")))

    def test_the_cgroup_parent_comes_from_the_variable(self) -> None:
        opts = str(self._buildx_step().get("with", {}).get("driver-opts", ""))
        assert "vars.HYPERCI_BUILDX_CGROUP_PARENT" in opts
        assert "cgroup-parent=" in opts

    def test_it_is_off_unless_the_variable_is_set(self) -> None:
        # The stock-dind fleet must keep the builder where it is today.
        opts = str(self._buildx_step().get("with", {}).get("driver-opts", ""))
        assert opts.rstrip().endswith("|| '' }}"), opts


SKIP_OPTIMIZE_ENV = "${{ inputs.skip-optimize || vars.HYPERCI_SKIP_OPTIMIZE }}"


class TestSkipOptimizeThreading:
    """issue #132: the skip-optimize switch reaches the build stage in every
    language workflow, so a consumer writes the same input whatever it builds."""

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_workflow_call_accepts_skip_optimize(self, workflow_name: str) -> None:
        wf = _load_workflow(workflow_name)
        on = wf.get("on") or wf.get(True, {})
        spec = on.get("workflow_call", {}).get("inputs", {}).get("skip-optimize")
        assert spec is not None, f"{workflow_name}: workflow_call missing skip-optimize"
        # A string input is "" when unset, so `inputs.x || vars.X` falls
        # through to the variable; a boolean false would not.
        assert spec.get("type") == "string"
        assert spec.get("default") == "", (
            f"{workflow_name}: skip-optimize must default to '' -- optimisation "
            "stays ON unless a run asks otherwise"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_dispatch_accepts_skip_optimize(self, workflow_name: str) -> None:
        wf = _load_workflow(workflow_name)
        on = wf.get("on") or wf.get(True, {})
        spec = on.get("workflow_dispatch", {}).get("inputs", {}).get("skip-optimize")
        assert spec is not None, (
            f"{workflow_name}: workflow_dispatch missing skip-optimize"
        )
        assert spec.get("type") == "string"
        assert spec.get("required") is not True, (
            f"{workflow_name}: skip-optimize must be optional"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_env_carries_skip_optimize_to_every_stage(self, workflow_name: str) -> None:
        # Workflow-level env: the build stage reads it through
        # hyperi_ci.common.skip_optimize and a later stage inherits it.
        wf = _load_workflow(workflow_name)
        assert wf.get("env", {}).get("HYPERCI_SKIP_OPTIMIZE") == SKIP_OPTIMIZE_ENV, (
            f"{workflow_name}: HYPERCI_SKIP_OPTIMIZE must be exactly {SKIP_OPTIMIZE_ENV}"
        )

    def test_release_tail_reads_the_variable_too(self) -> None:
        # The callers pass the raw input, so a repo-variable skip reaches the
        # tail only through its own env. Without it the image label and the
        # release notes call an unoptimised binary optimised.
        wf = _load_workflow("_release-tail.yml")
        assert wf.get("env", {}).get("HYPERCI_SKIP_OPTIMIZE") == SKIP_OPTIMIZE_ENV, (
            f"_release-tail.yml: HYPERCI_SKIP_OPTIMIZE must be exactly "
            f"{SKIP_OPTIMIZE_ENV}"
        )


RELEASE_UNOPTIMIZED_ENV = "${{ inputs.release-unoptimized }}"


class TestReleaseUnoptimizedThreading:
    """issue #158: releasing a skipped-optimisation build is its own per-run
    consent, threaded through every language workflow like skip-optimize."""

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    @pytest.mark.parametrize("trigger", ["workflow_call", "workflow_dispatch"])
    def test_accepts_the_input(self, workflow_name: str, trigger: str) -> None:
        wf = _load_workflow(workflow_name)
        on = wf.get("on") or wf.get(True, {})
        spec = on.get(trigger, {}).get("inputs", {}).get("release-unoptimized")
        assert spec is not None, (
            f"{workflow_name}: {trigger} missing release-unoptimized"
        )
        assert spec.get("type") == "string"
        assert spec.get("default") == "", "consent must be off unless a run asks"
        assert spec.get("required") is not True

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_env_reads_the_input_and_no_variable(self, workflow_name: str) -> None:
        # A repo variable would make the consent permanent, which is the
        # implicit release this switch exists to prevent.
        wf = _load_workflow(workflow_name)
        value = wf.get("env", {}).get("HYPERCI_RELEASE_UNOPTIMIZED")
        assert value == RELEASE_UNOPTIMIZED_ENV, (
            f"{workflow_name}: HYPERCI_RELEASE_UNOPTIMIZED must be exactly "
            f"{RELEASE_UNOPTIMIZED_ENV}"
        )


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

    def test_predict_first_release_uses_the_declared_version(self) -> None:
        # Tag-less repo: the starting version comes from the project's own
        # manifest via seed_version.py, never from the committed VERSION file
        # (issue #85 -- that file is an artefact, frozen in every repo).
        run = str(self._predict_step()["run"])
        # Polarity: only on a genuinely tag-less repo.
        assert '-z "$tags_all"' in run, (
            "seed override must fire only on a tag-less repo"
        )
        assert "seed_version.py" in run, (
            "starting version must come from the shared seed resolver"
        )
        assert "-f VERSION" not in run, (
            "predict must not read the VERSION file as an input (issue #85)"
        )

    def test_seed_resolver_ships_with_the_action(self) -> None:
        # The composite loads it by path out of its own checkout; a rename
        # would leave the step calling a file that is not there.
        path = (
            Path(__file__).resolve().parents[2]
            / ".github/actions/predict-version/seed_version.py"
        )
        assert path.is_file(), f"{path} is referenced by action.yml but missing"

    def test_predict_early_collision_guard(self) -> None:
        # Plan-time twin of the _release-tail off-HEAD guard: a predicted
        # version whose tag already exists off-HEAD fails before the build.
        run = str(self._predict_step()["run"])
        assert "refs/tags/v${version}^{commit}" in run, (
            "predict step must check the predicted tag for an off-HEAD collision"
        )

    def test_predict_guard_ordering(self) -> None:
        # The guards and the VERSION override must all run BEFORE the
        # version is emitted to GITHUB_OUTPUT -- substring presence alone
        # would stay green if a refactor moved a guard after the emit,
        # silently disarming it.
        run = str(self._predict_step()["run"])
        emit = run.index('echo "version=$version"')
        assert run.index("--merged HEAD") < emit, (
            "orphan guard must run before the version is emitted"
        )
        assert run.index("seed_version.py") < emit, (
            "seed override must apply before the version is emitted"
        )
        assert run.index("refs/tags/v${version}^{commit}") < emit, (
            "collision guard must run before the version is emitted"
        )
        # The override rewrites $version, so the collision guard must
        # check the FINAL value: override strictly before collision guard.
        assert run.index("seed_version.py") < run.index(
            "refs/tags/v${version}^{commit}"
        ), "collision guard must check the post-override version"

    def test_release_tail_first_release_uses_tag_head(self) -> None:
        # On a tag-less repo the real semantic-release run would tag its
        # own 1.0.0 default, diverging from the plan's resolved starting
        # version. The tail must materialise the plan's next-version via
        # tag-head instead -- one version oracle.
        wf = _load_workflow("_release-tail.yml")
        steps = wf["jobs"]["tag-and-release"]["steps"]
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
        # tag-head goes through `gh api` -- the step needs GH_TOKEN.
        assert "GH_TOKEN" in sr.get("env", {}), (
            "Tag step must export GH_TOKEN for tag-head's gh api call"
        )


class TestReleaseTailDecoupling:
    """issue #33: a Container failure must not block the primary publish,
    and a library must not boot Buildx / touch GHCR at all."""

    def _tail(self) -> dict:
        return _load_workflow("_release-tail.yml")

    def test_tag_and_release_decoupled_from_container(self) -> None:
        # `always()` ensures Tag & Release runs even when Container fails
        # or is skipped -- the crate/GH release is never lost to a
        # transient container hiccup.
        job = self._tail()["jobs"]["tag-and-release"]
        assert "always()" in str(job["if"]), (
            "tag-and-release must use always() so a failed/skipped Container "
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
        # gate skips non-publish main pushes - exactly where merge-to-main
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
    """Plan job MUST call the predict-version composite action -- not a
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
        f"gate decision is forbidden — see docs/architecture.md."
    )


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
def test_release_tail_uses_shared_workflow(workflow_name: str) -> None:
    """Release tail MUST be the shared `_release-tail.yml` workflow.
    This is the second of two allowed indirection layers (per
    docs/architecture.md).
    """
    wf = _load_workflow(workflow_name)
    jobs = wf.get("jobs", {})
    # Look for any job that calls _release-tail.yml
    tail_calls = [
        job for job in jobs.values() if "_release-tail.yml" in str(job.get("uses", ""))
    ]
    assert tail_calls, (
        f"{workflow_name}: no job uses _release-tail.yml. "
        f"Every language workflow must delegate container + tag-and-release "
        f"to the shared release-tail workflow."
    )


class TestRunnerSelection:
    """The `runs-on` expression is duplicated per language, so it drifts.

    Runner NAMES are not asserted here and must not be: they live in the
    `GH_RUNNER_*` org variables, which is the only place that can hold them --
    GitHub evaluates `runs-on` before any of our code runs (issue #99). What is
    duplicated, and therefore what this guards, is the fallback chain.
    """

    # Every gated runs-on ends the same way, so an unset variable always lands
    # somewhere real.
    TAIL = "vars.GH_RUNNER_DEFAULT || 'ubuntu-latest' }}"

    # `free` mode is what lets an org with no self-hosted fleet use these
    # workflows; losing it strands them on a runner that never answers.
    FREE_MODE = (
        "(inputs.runner-mode || vars.GH_RUNNER_MODE) == 'free' && 'ubuntu-latest'"
    )

    RENOVATE = "startsWith(github.head_ref || github.ref_name, 'renovate/')"

    LANG_VAR = {
        "rust-ci.yml": "GH_RUNNER_RUST",
        "python-ci.yml": "GH_RUNNER_PYTHON",
        "go-ci.yml": "GH_RUNNER_GOLANG",
        "ts-ci.yml": "GH_RUNNER_TYPESCRIPT",
    }

    @staticmethod
    def _gated_runs_on(workflow_name: str) -> list[tuple[str, str]]:
        """Every (job, runs-on) that resolves through an org variable."""
        wf = _load_workflow(workflow_name)
        found = []
        for name, job in wf.get("jobs", {}).items():
            runs_on = str(job.get("runs-on", ""))
            if "GH_RUNNER" in runs_on:
                found.append((name, runs_on))
        return found

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_every_language_gates_at_least_one_job(self, workflow_name: str) -> None:
        assert self._gated_runs_on(workflow_name), (
            f"{workflow_name}: no job resolves its runner through GH_RUNNER_* — "
            "the org variables are the SSoT and something now bypasses them"
        )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_the_fallback_chain_always_terminates(self, workflow_name: str) -> None:
        for job, runs_on in self._gated_runs_on(workflow_name):
            assert self.TAIL in runs_on, (
                f"{workflow_name}.{job}: runs-on must end with "
                f"{self.TAIL!r} so an unset variable still lands on a real "
                f"runner.\n  actual: {runs_on}"
            )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_free_mode_escape_is_present(self, workflow_name: str) -> None:
        for job, runs_on in self._gated_runs_on(workflow_name):
            assert self.FREE_MODE in runs_on, (
                f"{workflow_name}.{job}: runs-on lost the `free` mode escape, "
                f"so an org with no self-hosted fleet queues forever.\n"
                f"  actual: {runs_on}"
            )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_renovate_carveout_is_present(self, workflow_name: str) -> None:
        for job, runs_on in self._gated_runs_on(workflow_name):
            assert self.RENOVATE in runs_on, (
                f"{workflow_name}.{job}: runs-on lost the renovate carve-out "
                f"(issue #91).\n  actual: {runs_on}"
            )

    @pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
    def test_each_language_uses_its_own_variable(self, workflow_name: str) -> None:
        # Copy-paste between the four workflows is the drift mode: a python job
        # resolving through GH_RUNNER_RUST lands on the wrong image and the
        # symptom is a missing toolchain, not a wrong-runner error.
        mine = self.LANG_VAR[workflow_name]
        theirs = {v for k, v in self.LANG_VAR.items() if k != workflow_name}
        for job, runs_on in self._gated_runs_on(workflow_name):
            assert mine in runs_on, (
                f"{workflow_name}.{job}: runs-on does not reference {mine}"
            )
            for other in theirs:
                # GH_RUNNER_RUST is a substring of GH_RUNNER_RENOVATE_RUST, so
                # match the variable reference rather than the bare name.
                assert f"vars.{other}" not in runs_on, (
                    f"{workflow_name}.{job}: runs-on references {other}, which "
                    f"belongs to another language.\n  actual: {runs_on}"
                )


def test_the_container_build_resolves_its_runner_through_the_org_variables() -> None:
    """issue #80: the tail hardcoded ubuntu-latest, so GH_RUNNER_PUBLISH was set
    org-wide and read by nothing while the multi-arch container build paid for
    hosted minutes. TestRunnerSelection only covers the four language workflows,
    which is why this went unseen.

    The container build is the whole saving, so it is the only job asserted
    here; tag-and-release cannot run there at all (next test).

    No renovate carve-out here on purpose: the tail gates on will-publish and
    push-to-main, so it never runs on a renovate/ branch.
    """
    runs_on = str(_load_workflow("_release-tail.yml")["jobs"]["container"]["runs-on"])
    assert "vars.GH_RUNNER_PUBLISH" in runs_on, (
        f"_release-tail.container: runs-on must resolve through "
        f"GH_RUNNER_PUBLISH.\n  actual: {runs_on}"
    )
    assert "vars.GH_RUNNER_DEFAULT || 'ubuntu-latest' }}" in runs_on, (
        f"_release-tail.container: the fallback chain must terminate on a real "
        f"runner.\n  actual: {runs_on}"
    )
    assert "vars.GH_RUNNER_MODE == 'free' && 'ubuntu-latest'" in runs_on, (
        f"_release-tail.container: lost the free-mode escape, so an org with "
        f"no self-hosted fleet queues forever.\n  actual: {runs_on}"
    )


def test_the_publish_job_resolves_a_runner_like_the_container_job() -> None:
    """Both tail jobs pick a runner the same way.

    This job was pinned to `ubuntu-latest` while the ARC images carried no aws
    CLI and the R2 upload needed one. `native_deps.ensure_aws_cli` installs
    AWS's signed v2 bundle on demand, so the pin is gone; apt cannot supply it,
    since noble offers no awscli candidate.
    """
    runs_on = str(
        _load_workflow("_release-tail.yml")["jobs"]["tag-and-release"]["runs-on"]
    )
    assert "vars.GH_RUNNER_PUBLISH" in runs_on, (
        f"_release-tail.tag-and-release: runs-on must resolve through "
        f"GH_RUNNER_PUBLISH.\n  actual: {runs_on}"
    )
    assert "vars.GH_RUNNER_DEFAULT || 'ubuntu-latest' }}" in runs_on, (
        f"_release-tail.tag-and-release: the fallback chain must terminate on a "
        f"real runner.\n  actual: {runs_on}"
    )
    assert "vars.GH_RUNNER_MODE == 'free' && 'ubuntu-latest'" in runs_on, (
        f"_release-tail.tag-and-release: lost the free-mode escape, so an org "
        f"with no self-hosted fleet queues forever.\n  actual: {runs_on}"
    )


def test_every_workflow_pins_the_cli_interpreter() -> None:
    """issue #157: with nothing pinning it, uvx hands the CLI the project's
    Python and silently installs an older hyperi-ci that allowed that version
    instead of failing.

    ci-test-python-lib (floor >=3.12) ran 2.9.28 while main was on 2.10.2, and
    its quality gate failed on a bug that had already been fixed.
    """
    from hyperi_ci.versions import runtime_version

    expected = f"--python {runtime_version('python')}"
    for name in (
        "python-ci.yml",
        "rust-ci.yml",
        "go-ci.yml",
        "ts-ci.yml",
        "_release-tail.yml",
    ):
        install = str(_load_workflow(name)["env"]["HYPERCI_INSTALL"])
        assert expected in install, (
            f"{name}: HYPERCI_INSTALL must pin the CLI's own interpreter with "
            f"`{expected}`.\n  actual: {install}"
        )


def test_rust_renovate_carveout_never_lands_on_a_toolchainless_runner() -> None:
    """issue #91: renovate/ branches must resolve to a runner with cargo.

    GH_RUNNER_RENOVATE and GH_RUNNER_DEFAULT point at vanilla sets, and Rust
    does not bootstrap its toolchain at job time -- so rust-ci.yml's renovate
    carve-out must chain GH_RUNNER_RENOVATE_RUST -> GH_RUNNER_RUST, never the
    shared GH_RUNNER_RENOVATE.
    """
    import re

    text = (WORKFLOW_DIR / "rust-ci.yml").read_text(encoding="utf-8")
    expressions = [
        line for line in text.splitlines() if "renovate/" in line and "${{" in line
    ]
    assert expressions, "rust-ci.yml: the renovate carve-out has vanished entirely"
    for line in expressions:
        assert not re.search(r"GH_RUNNER_RENOVATE(?!_RUST)", line), (
            f"rust-ci.yml routes a renovate/ branch through the shared "
            f"GH_RUNNER_RENOVATE (a vanilla set with no cargo): {line.strip()}"
        )
        assert "GH_RUNNER_RUST" in line, (
            f"rust-ci.yml renovate carve-out must fall back to GH_RUNNER_RUST: "
            f"{line.strip()}"
        )


# Steps in _release-tail.yml that PUSH to the default branch or create a tag.
# Named by the `name:` field so a reordering does not silently drop one.
_PUSHING_STEPS = (
    "Tag (semantic-release)",
    "Tag HEAD (forced bump / explicit version)",
    "Commit rendered release artefacts",
)

_BOT_TOKEN = "${{ steps.bot.outputs.token || secrets.GITHUB_TOKEN }}"


def _tag_and_release_steps() -> list[dict]:
    wf = _load_workflow("_release-tail.yml")
    return wf["jobs"]["tag-and-release"]["steps"]


def test_release_tail_mints_a_bot_token() -> None:
    """The App token must be minted from the Client ID, not the App ID.

    `app-id` is deprecated in actions/create-github-app-token, and the Client
    ID is a different value rather than a rename, so a swap back would fail at
    token-mint time instead of at review.
    """
    steps = _tag_and_release_steps()
    mint = [s for s in steps if s.get("id") == "bot"]
    assert mint, (
        "_release-tail.tag-and-release: no `bot` step minting an App token. "
        "Pushes made as github-actions cannot be excepted from a branch "
        "ruleset and trigger no workflows (issue #86)."
    )
    with_ = mint[0].get("with", {})
    assert "client-id" in with_, "the bot token must be minted from client-id"
    assert "app-id" not in with_, "app-id is deprecated — use client-id"


def _app_token_steps() -> list[tuple[str, str, dict]]:
    """Every create-github-app-token step in every workflow here.

    Found by CONTENT rather than by naming the files that mint one today, so a
    workflow added later is covered without anyone remembering to list it
    (the #98 lesson).
    """
    found = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (wf.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if "create-github-app-token" in str(step.get("uses", "")):
                    found.append((path.name, job_name, step))
    return found


def test_some_workflow_mints_an_app_token() -> None:
    # Guards the guard: if the discovery ever returns nothing, the assertions
    # below become vacuous and would pass forever.
    assert _app_token_steps(), "no workflow mints an App token — discovery broke"


def test_no_workflow_mints_an_app_token_from_the_app_id() -> None:
    """`app-id` is deprecated in actions/create-github-app-token (issue #100).

    The numeric App ID is not deprecated at the platform level, but the INPUT
    is, and it warns on every run. The Client ID is a different value rather
    than a rename, so a swap back fails at mint time rather than at review.
    """
    for workflow, job, step in _app_token_steps():
        with_ = step.get("with", {})
        assert "app-id" not in with_, (
            f"{workflow}.{job}: mints an App token from the deprecated `app-id` "
            f"input. Use `client-id: ${{{{ vars.GH_APP_CLIENT_ID }}}}`."
        )
        assert "client-id" in with_, (
            f"{workflow}.{job}: App token step has neither client-id nor app-id"
        )


@pytest.mark.parametrize("step_name", _PUSHING_STEPS)
def test_pushing_steps_use_the_bot_token(step_name: str) -> None:
    """Every step that writes to the repo must prefer the App token.

    A bare `secrets.GITHUB_TOKEN` here is the regression that reintroduces
    issue #86: the push lands as github-actions, which no ruleset can grant a
    bypass to, so the repo cannot carry required status checks.
    """
    steps = _tag_and_release_steps()
    matching = [s for s in steps if s.get("name") == step_name]
    assert matching, f"_release-tail: step {step_name!r} has gone or been renamed"
    env = matching[0].get("env", {})
    tokens = {k: v for k, v in env.items() if k in ("GH_TOKEN", "GITHUB_TOKEN")}
    assert tokens, f"{step_name}: expected a GH_TOKEN/GITHUB_TOKEN in env"
    for key, value in tokens.items():
        assert value == _BOT_TOKEN, (
            f"{step_name}: {key} is {value!r}, expected the bot token with a "
            f"GITHUB_TOKEN fallback ({_BOT_TOKEN!r})."
        )


# Every workflow whose plan job feeds the checks gate, hyperi-ci's own
# included -- a fork PR must reach `plan` in all of them.
_PLAN_WORKFLOWS = (*LANGUAGE_WORKFLOWS, "ci.yml")


@pytest.mark.parametrize("workflow_name", _PLAN_WORKFLOWS)
def test_plan_job_runs_on_a_fork_pr(workflow_name: str) -> None:
    """The plan job must not exclude fork PRs (issue #176).

    Every downstream gate reads `needs.plan.outputs.run-checks`, which is
    empty when plan skips, so a fork PR that cannot reach plan runs no
    quality and no test -- and a skipped required check still satisfies
    branch protection, so it merges green.
    """
    wf = _load_workflow(workflow_name)
    plan = wf["jobs"]["plan"]
    condition = str(plan.get("if", ""))
    assert "fork" not in condition, (
        f"{workflow_name}: the plan job gates on {condition!r}, so a fork PR "
        f"skips it and every check downstream. predict-version only needs "
        f"write access on a push or dispatch, never on a PR."
    )


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
def test_docker_hub_login_is_fork_guarded(workflow_name: str) -> None:
    """A Docker Hub login step must skip on a fork PR.

    `vars` are readable by a fork PR and `secrets` are not, so a step gated
    only on `vars.DOCKERHUB_USERNAME` authenticates with an empty password
    and fails the job it sits in.
    """
    wf = _load_workflow(workflow_name)
    seen = 0
    for job_name, job in wf["jobs"].items():
        for step in job.get("steps", []):
            if "DOCKERHUB_USERNAME" not in str(step.get("if", "")):
                continue
            seen += 1
            assert "fork" in str(step["if"]), (
                f"{workflow_name}.{job_name}: Docker Hub login gates only on "
                f"the var, which a fork PR can read while the secret stays "
                f"empty -- the login fails and takes the job with it."
            )
    assert seen, f"{workflow_name}: no Docker Hub login step found to check"


@pytest.mark.parametrize("workflow_name", _PLAN_WORKFLOWS)
def test_a_terminal_gate_job_always_runs(workflow_name: str) -> None:
    """Every workflow publishes a context a skipped check cannot satisfy.

    Branch protection requiring `ci / Quality` is satisfied by Quality
    skipping, so the doctrine's deliberate skip and a gate that never fired
    look identical from outside the run (issue #177).
    """
    wf = _load_workflow(workflow_name)
    gate = wf["jobs"].get("gate")
    assert gate, f"{workflow_name}: no terminal gate job -- a skip can pass the run"
    assert "always()" in str(gate.get("if", "")), (
        f"{workflow_name}: the gate job is conditional, so it can skip with "
        f"everything else and satisfy branch protection by doing nothing."
    )
    for upstream in ("plan", "quality", "test", "build"):
        assert upstream in gate["needs"], (
            f"{workflow_name}: the gate does not need {upstream!r}, so it "
            f"cannot see whether it ran."
        )
