# Project:   HyperI CI
# File:      tests/unit/test_setup_runtime_uv.py
# Purpose:   Every consumer-run setup-uv install retries on a network blip
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The setup-uv install steps a consumer's run executes.

setup-uv reads its version manifest from raw.githubusercontent.com on every
cold runner, so one network blip fails the step. Each install is three
attempts in pure YAML. Only the last may fail the job, so a persistent outage
still goes red. An install that was already tolerant (predict-version) stays
tolerant on every attempt.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent.parent / ".github"
SOURCES = (
    "actions/setup-runtime/action.yml",
    "actions/predict-version/action.yml",
    "workflows/_release-tail.yml",
    "workflows/rust-ci.yml",
    "workflows/go-ci.yml",
    "workflows/ts-ci.yml",
)
SETUP_UV = "astral-sh/setup-uv@"


def _step_lists(rel: str) -> list[tuple[str, list[dict]]]:
    doc = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
    if "jobs" in doc:
        return [(f"{rel}:{n}", j.get("steps", [])) for n, j in doc["jobs"].items()]
    return [(rel, doc["runs"]["steps"])]


def _installs(steps: list[dict]) -> list[list[dict]]:
    """Group consecutive setup-uv steps into one install each."""
    groups: list[list[dict]] = []
    prev_uv = False
    for step in steps:
        is_uv = SETUP_UV in str(step.get("uses", ""))
        if is_uv and prev_uv and "(retry" in str(step.get("name")):
            groups[-1].append(step)
        elif is_uv:
            groups.append([step])
        prev_uv = is_uv
    return groups


def _all() -> list[tuple[str, list[dict]]]:
    return [
        (where, group)
        for rel in SOURCES
        for where, steps in _step_lists(rel)
        for group in _installs(steps)
    ]


def _ids() -> list[str]:
    return [f"{w}#{g[0]['name']}" for w, g in _all()]


@pytest.mark.parametrize(("where", "group"), _all(), ids=_ids())
class TestEveryInstallRetries:
    def test_three_attempts(self, where: str, group: list[dict]) -> None:
        assert len(group) == 3, where

    def test_first_attempt_keeps_its_condition_and_is_tolerated(
        self, where: str, group: list[dict]
    ) -> None:
        assert group[0]["continue-on-error"] is True, where
        assert group[0]["id"] == "uv_try1", where

    def test_each_retry_runs_only_after_the_previous_failed(
        self, where: str, group: list[dict]
    ) -> None:
        for prev, step in zip(group, group[1:], strict=False):
            assert str(step["if"]) == (
                "${{ steps." + prev["id"] + ".outcome == 'failure' }}"
            ), where

    def test_only_the_last_attempt_can_fail_the_job(
        self, where: str, group: list[dict]
    ) -> None:
        *tolerated, last = group
        assert all(s["continue-on-error"] is True for s in tolerated), where
        assert "id" not in last, where
        # predict-version is tolerant by design: a missing uv falls back loudly.
        if "predict-version" in where:
            assert last["continue-on-error"] is True
        else:
            assert "continue-on-error" not in last, where

    def test_attempts_use_the_same_action_and_inputs(
        self, where: str, group: list[dict]
    ) -> None:
        for step in group[1:]:
            assert step["uses"] == group[0]["uses"], where
            assert step.get("with") == group[0].get("with"), where


def test_the_expected_installs_are_all_found() -> None:
    # 1 setup-runtime + 1 predict-version + 4 release-tail + 3 per language CI.
    assert len(_all()) == 1 + 1 + 4 + 3 * 3


@pytest.mark.parametrize("rel", SOURCES)
def test_attempt_ids_are_unique_per_job(rel: str) -> None:
    for where, steps in _step_lists(rel):
        ids = [s["id"] for s in steps if "id" in s]
        assert len(ids) == len(set(ids)), where
