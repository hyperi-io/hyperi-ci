# Project:   HyperI CI
# File:      tests/unit/test_test_job_inputs.py
# Purpose:   Hold the Test job's token and timeout caller inputs to one shape
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The Test job's `test-github-token` and `test-timeout-minutes` inputs.

Both are opt-in per caller. With neither set, the Test job runs exactly as it
did before they existed: no GitHub token in the test step's environment, and
GitHub's own 360-minute job limit.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).parent.parent.parent / ".github" / "workflows"
LANGUAGE_WORKFLOWS = ("rust-ci.yml", "python-ci.yml", "ts-ci.yml", "go-ci.yml")

RUN_TESTS = (
    "${{ env.HYPERCI_INSTALL }} run test "
    "${{ needs.plan.outputs.test-tier == 'full' && '--tier full' || '' }}"
)
JOB_TOKEN = "${{ github.token }}"
TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN")
GITHUB_JOB_LIMIT = 360
SELF_HOSTED_JOB_LIMIT = 5 * 24 * 60

# Anything that would put a GitHub credential in front of test code.
_CREDENTIAL = re.compile(r"github\.token|secrets\.GITHUB_TOKEN|GH_TOKEN|GITHUB_TOKEN")

# Steps that hold the job token without running tests: rust-ci.yml clones a
# caller's sibling-checkouts with it.
_MAY_HOLD_A_TOKEN = frozenset(
    {"Run tests with a GitHub token", "Clone sibling checkouts"}
)


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _call_inputs(wf: dict) -> dict:
    # PyYAML reads the bare key `on:` as the boolean True.
    on = wf.get("on") or wf.get(True, {})
    return on["workflow_call"]["inputs"]


def _test_steps(wf: dict) -> list[dict]:
    return [s for s in wf["jobs"]["test"]["steps"] if s.get("run") == RUN_TESTS]


def _step_runs(condition: str, value: str | None) -> bool:
    """Evaluate one of the two opt-in conditions the way GitHub does.

    GitHub compares strings case-insensitively, and a workflow_dispatch run
    of the reusable workflow has no such input, so the value is null there.
    """
    match = re.fullmatch(r"inputs\.test-github-token (==|!=) 'true'", condition)
    assert match, f"unexpected opt-in condition: {condition!r}"
    opted_in = value is not None and value.lower() == "true"
    return opted_in if match.group(1) == "==" else not opted_in


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
class TestGitHubTokenInput:
    def test_the_input_is_off_by_default(self, workflow_name: str) -> None:
        spec = _call_inputs(_load(workflow_name))["test-github-token"]
        # A string, like every other on/off input here: "" when unset.
        assert spec["type"] == "string"
        assert spec["default"] == ""
        assert spec.get("required") is not True
        assert "every test dependency can read it" in spec["description"]

    def test_exactly_one_test_step_runs_for_any_value(self, workflow_name: str) -> None:
        steps = _test_steps(_load(workflow_name))
        assert len(steps) == 2, (
            f"{workflow_name}: expected a plain and a token-carrying Run tests step"
        )
        for value in (None, "", "false", "true", "TRUE", "yes"):
            running = [s["name"] for s in steps if _step_runs(s["if"], value)]
            assert len(running) == 1, f"{workflow_name}: {value!r} runs {running}"

    def test_only_the_opted_in_step_carries_the_token(self, workflow_name: str) -> None:
        steps = {s["name"]: s for s in _test_steps(_load(workflow_name))}
        plain = steps["Run tests"]
        with_token = steps["Run tests with a GitHub token"]
        assert _step_runs(plain["if"], "") and not _step_runs(plain["if"], "true")
        assert _step_runs(with_token["if"], "true")
        assert not _step_runs(with_token["if"], "")
        assert "env" not in plain
        assert with_token["env"] == dict.fromkeys(TOKEN_VARIABLES, JOB_TOKEN)

    def test_no_other_part_of_the_test_job_sees_a_credential(
        self, workflow_name: str
    ) -> None:
        test = _load(workflow_name)["jobs"]["test"]
        assert "env" not in test
        others = [s for s in test["steps"] if s.get("name") not in _MAY_HOLD_A_TOKEN]
        leaks = [
            s.get("name") or s.get("uses") for s in others if _CREDENTIAL.search(str(s))
        ]
        assert not leaks, f"{workflow_name}: a credential reaches {leaks}"

    def test_the_token_stays_read_only(self, workflow_name: str) -> None:
        wf = _load(workflow_name)
        assert wf["permissions"] == {"contents": "read"}
        assert "permissions" not in wf["jobs"]["test"], (
            f"{workflow_name}: the Test job must not raise the job token's scope"
        )


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
class TestTimeoutInput:
    def test_the_default_changes_nothing(self, workflow_name: str) -> None:
        spec = _call_inputs(_load(workflow_name))["test-timeout-minutes"]
        assert spec["type"] == "number"
        assert spec["default"] == GITHUB_JOB_LIMIT
        assert spec.get("required") is not True
        assert str(SELF_HOSTED_JOB_LIMIT) in spec["description"]

    def test_the_test_job_reads_it_with_the_same_fallback(
        self, workflow_name: str
    ) -> None:
        # The fallback covers a dispatch (no input, so null) and a caller's 0.
        # It must be the input's own default, or the two paths disagree.
        minutes = _load(workflow_name)["jobs"]["test"]["timeout-minutes"]
        assert minutes == (
            f"${{{{ inputs.test-timeout-minutes || {GITHUB_JOB_LIMIT} }}}}"
        )


def test_every_workflow_declares_both_inputs_identically() -> None:
    specs = [_call_inputs(_load(name)) for name in LANGUAGE_WORKFLOWS]
    for key in ("test-github-token", "test-timeout-minutes"):
        assert all(s[key] == specs[0][key] for s in specs), f"{key} drifted"


def test_the_dispatch_schema_is_untouched() -> None:
    # Both are per-repo settings a caller writes in `with:`, never per-run
    # dispatch inputs, so no consumer's dispatch schema has to grow.
    for name in LANGUAGE_WORKFLOWS:
        wf = _load(name)
        on = wf.get("on") or wf.get(True, {})
        dispatch = on["workflow_dispatch"]["inputs"]
        assert "test-github-token" not in dispatch
        assert "test-timeout-minutes" not in dispatch
