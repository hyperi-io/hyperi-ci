# Project:   HyperI CI
# File:      tests/unit/test_setup_runtime_uv.py
# Purpose:   setup-runtime pins uv and retries its install on a network blip
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The "Install uv" steps of the setup-runtime composite.

setup-uv reads its version manifest from raw.githubusercontent.com even for a
pinned version, so one network blip fails the step. The composite runs three
attempts in pure YAML. Only the last may fail the job, so a persistent outage
still goes red.
"""

from pathlib import Path

import yaml

from hyperi_ci.versions import tool_version

ACTION = (
    Path(__file__).parent.parent.parent
    / ".github"
    / "actions"
    / "setup-runtime"
    / "action.yml"
)


def _action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def _uv_steps() -> list[dict]:
    steps = [
        s
        for s in _action()["runs"]["steps"]
        if "astral-sh/setup-uv" in str(s.get("uses", ""))
    ]
    assert steps, "setup-runtime has no setup-uv step"
    return steps


class TestUvPin:
    def test_default_matches_versions_yaml(self) -> None:
        default = _action()["inputs"]["uv-version"]["default"]
        assert str(default) == tool_version("uv")

    def test_every_attempt_passes_the_pinned_version(self) -> None:
        for step in _uv_steps():
            assert step["with"]["version"] == "${{ inputs.uv-version }}", step["name"]


class TestUvRetry:
    def test_three_attempts(self) -> None:
        assert len(_uv_steps()) == 3

    def test_first_attempt_is_tolerated_and_hosted_only(self) -> None:
        first = _uv_steps()[0]
        assert first["continue-on-error"] is True
        assert first["id"]
        assert "self-hosted" in str(first["if"])

    def test_each_retry_runs_only_after_the_previous_failed(self) -> None:
        steps = _uv_steps()
        for prev, step in zip(steps, steps[1:], strict=False):
            assert str(step["if"]) == (
                "${{ steps." + prev["id"] + ".outcome == 'failure' }}"
            ), step["name"]

    def test_only_the_last_attempt_can_fail_the_job(self) -> None:
        *tolerated, last = _uv_steps()
        assert all(s.get("continue-on-error") is True for s in tolerated)
        assert "continue-on-error" not in last

    def test_attempts_use_the_same_action_and_inputs(self) -> None:
        steps = _uv_steps()
        for step in steps[1:]:
            assert step["uses"] == steps[0]["uses"]
            assert step["with"] == steps[0]["with"]
