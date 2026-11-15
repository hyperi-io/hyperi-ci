# Project:   HyperI CI
# File:      tests/unit/test_setup_runtime_uv.py
# Purpose:   Every consumer-run setup-uv install retries on a network blip
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The setup-uv install steps a consumer's run executes.

setup-uv reads its version manifest from raw.githubusercontent.com on every
cold runner, so one network blip fails the step. The setup-uv composite makes
three attempts in pure YAML, and only the last may fail the job, so a
persistent outage still goes red. Every consumer-run install calls it.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent.parent / ".github"
COMPOSITE = ROOT / "actions/setup-uv/action.yml"
CALL = "hyperi-io/hyperi-ci/.github/actions/setup-uv@main"
SOURCES = (
    "actions/setup-runtime/action.yml",
    "actions/predict-version/action.yml",
    "workflows/_release-tail.yml",
    "workflows/rust-ci.yml",
    "workflows/go-ci.yml",
    "workflows/python-ci.yml",
    "workflows/ts-ci.yml",
    "workflows/iac-ci.yml",
)


def _step_lists(rel: str) -> list[tuple[str, list[dict]]]:
    doc = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
    if "jobs" in doc:
        return [(f"{rel}:{n}", j.get("steps", [])) for n, j in doc["jobs"].items()]
    return [(rel, doc["runs"]["steps"])]


def _calls() -> list[tuple[str, dict]]:
    return [
        (where, step)
        for rel in SOURCES
        for where, steps in _step_lists(rel)
        for step in steps
        if str(step.get("name", "")).startswith("Install uv")
    ]


class TestTheCompositeRetries:
    @pytest.fixture
    def attempts(self) -> list[dict]:
        doc = yaml.safe_load(COMPOSITE.read_text(encoding="utf-8"))
        return doc["runs"]["steps"]

    def test_three_attempts_of_one_action_and_inputs(
        self, attempts: list[dict]
    ) -> None:
        assert len(attempts) == 3
        assert all(a["uses"].startswith("astral-sh/setup-uv@") for a in attempts)
        assert all(a["uses"] == attempts[0]["uses"] for a in attempts)
        assert all(a["with"] == attempts[0]["with"] for a in attempts)

    def test_each_retry_runs_only_after_the_previous_failed(
        self, attempts: list[dict]
    ) -> None:
        for prev, step in zip(attempts, attempts[1:], strict=False):
            assert step["if"] == "${{ steps." + prev["id"] + ".outcome == 'failure' }}"

    def test_only_the_last_attempt_can_fail(self, attempts: list[dict]) -> None:
        *tolerated, last = attempts
        assert all(s["continue-on-error"] is True for s in tolerated)
        assert "continue-on-error" not in last


@pytest.mark.parametrize(("where", "step"), _calls(), ids=[w for w, _ in _calls()])
def test_every_install_calls_the_composite(where: str, step: dict) -> None:
    assert step["uses"] == CALL, where


def test_the_expected_installs_are_all_found() -> None:
    # setup-runtime, predict-version, 6 in the tail, 3 in each of 4 language
    # CIs, and iac-ci.
    assert len(_calls()) == 1 + 1 + 6 + 3 * 4 + 1


@pytest.mark.parametrize("rel", SOURCES)
def test_no_source_calls_setup_uv_directly(rel: str) -> None:
    for where, steps in _step_lists(rel):
        direct = [s for s in steps if "astral-sh/setup-uv@" in str(s.get("uses", ""))]
        assert not direct, where


def test_only_predict_version_tolerates_a_failed_install() -> None:
    # A missing uv there falls back to python3, loudly.
    tolerant = [w for w, s in _calls() if s.get("continue-on-error") is True]
    assert tolerant == ["actions/predict-version/action.yml"]
