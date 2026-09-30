# Project:   HyperI CI
# File:      tests/unit/test_build_job_inputs.py
# Purpose:   Hold the Build job's timeout caller input to one shape
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The Build job's `build-timeout-minutes` input.

Opt-in per caller. A caller that sets nothing keeps the 135-minute limit the
Build job carried before the input existed.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).parent.parent.parent / ".github" / "workflows"
LANGUAGE_WORKFLOWS = ("rust-ci.yml", "python-ci.yml", "ts-ci.yml", "go-ci.yml")

BUILD_JOB_LIMIT = 135
GITHUB_HOSTED_JOB_LIMIT = 360
SELF_HOSTED_JOB_LIMIT = 5 * 24 * 60


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _on(wf: dict) -> dict:
    # PyYAML reads the bare key `on:` as the boolean True.
    return wf.get("on") or wf.get(True, {})


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
class TestBuildTimeoutInput:
    def test_the_default_changes_nothing(self, workflow_name: str) -> None:
        spec = _on(_load(workflow_name))["workflow_call"]["inputs"][
            "build-timeout-minutes"
        ]
        assert spec["type"] == "number"
        assert spec["default"] == BUILD_JOB_LIMIT
        assert spec.get("required") is not True
        assert str(GITHUB_HOSTED_JOB_LIMIT) in spec["description"]
        assert str(SELF_HOSTED_JOB_LIMIT) in spec["description"]

    def test_the_build_job_reads_it_with_the_same_fallback(
        self, workflow_name: str
    ) -> None:
        # The fallback covers a dispatch (no input, so null) and a caller's 0.
        # It must be the input's own default, or the two paths disagree.
        minutes = _load(workflow_name)["jobs"]["build"]["timeout-minutes"]
        assert minutes == (
            f"${{{{ inputs.build-timeout-minutes || {BUILD_JOB_LIMIT} }}}}"
        )


def test_every_workflow_declares_the_input_identically() -> None:
    specs = [
        _on(_load(name))["workflow_call"]["inputs"]["build-timeout-minutes"]
        for name in LANGUAGE_WORKFLOWS
    ]
    assert all(s == specs[0] for s in specs), "build-timeout-minutes drifted"


def test_the_dispatch_schema_is_untouched() -> None:
    # A per-repo setting a caller writes in `with:`, never a per-run dispatch
    # input, so no consumer's dispatch schema has to grow.
    for name in LANGUAGE_WORKFLOWS:
        dispatch = _on(_load(name))["workflow_dispatch"]["inputs"]
        assert "build-timeout-minutes" not in dispatch
