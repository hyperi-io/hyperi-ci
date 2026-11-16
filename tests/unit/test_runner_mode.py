# Project:   HyperI CI
# File:      tests/unit/test_runner_mode.py
# Purpose:   Tests for the Plan's runner-mode resolution
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The runner mode every later job routes by: explicit, then visibility."""

import importlib.util
from pathlib import Path

import pytest

HELPER = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "actions"
    / "predict-version"
    / "runner_mode.py"
)
_spec = importlib.util.spec_from_file_location("runner_mode_helper", HELPER)
assert _spec and _spec.loader
runner_mode = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner_mode)


def _never() -> str:
    raise AssertionError("the API must not be asked")


@pytest.mark.parametrize(
    ("explicit", "payload", "api", "mode"),
    [
        pytest.param(
            "free", "private", _never, "free", id="explicit-free-beats-private"
        ),
        pytest.param(
            "self-hosted",
            "public",
            _never,
            "self-hosted",
            id="explicit-self-hosted-beats-public",
        ),
        pytest.param(
            "anything",
            "public",
            _never,
            "self-hosted",
            id="unknown-explicit-is-self-hosted",
        ),
        pytest.param("", "public", _never, "free", id="public-payload"),
        pytest.param("", "private", _never, "self-hosted", id="private-payload"),
        pytest.param("", "internal", _never, "self-hosted", id="internal-payload"),
        pytest.param("", "", lambda: "public", "free", id="no-payload-public-api"),
        pytest.param(
            "", "", lambda: "private", "self-hosted", id="no-payload-private-api"
        ),
        pytest.param("", "", lambda: "", "self-hosted", id="nothing-readable"),
    ],
)
def test_resolve(explicit: str, payload: str, api, mode: str) -> None:
    got, reason = runner_mode.resolve(explicit, payload, api)
    assert got == mode
    assert reason


@pytest.mark.parametrize(
    ("explicit", "payload"),
    [("self-hosted", "public"), ("", "private"), ("", ""), ("free", "public")],
)
def test_a_fork_pr_never_runs_self_hosted(explicit: str, payload: str) -> None:
    got, reason = runner_mode.resolve(explicit, payload, _never, fork_pr=True)
    assert (got, "fork" in reason) == ("free", True)


def test_the_fork_flag_reaches_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("RUNNER_MODE", "self-hosted")
    monkeypatch.setenv("REPO_VISIBILITY", "public")
    monkeypatch.setenv("FORK_PR", "true")
    runner_mode.run()
    assert out.read_text(encoding="utf-8") == "runner-mode=free\n"
