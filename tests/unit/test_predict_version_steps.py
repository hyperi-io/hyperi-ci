# Project:   HyperI CI
# File:      tests/unit/test_predict_version_steps.py
# Purpose:   Run the predict-version gate steps for real, per event
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Execute the composite's gate, derive and tier steps under bash.

String assertions on the step body cannot show what a scheduled run decides.
These render each step's ``${{ }}`` expressions for one event and run it in a
throwaway repo whose HEAD carries ``Release: true``, so a schedule branch that
fell through to the trailer check would publish and fail the test.

``python3`` on the step's PATH is this interpreter, so a helper needing
PyYAML does not depend on whatever the host's own python3 carries.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ACTION_DIR = (
    Path(__file__).resolve().parents[2] / ".github" / "actions" / "predict-version"
)
_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash and git",
)


def _steps() -> dict[str, dict]:
    action = yaml.safe_load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"))
    return {s["id"]: s for s in action["runs"]["steps"] if "id" in s}


def _render(text: str, values: dict[str, str]) -> str:
    def lookup(match: re.Match[str]) -> str:
        expression = match.group(1)
        if expression not in values:
            raise KeyError(f"no test value for ${{{{ {expression} }}}}")
        return values[expression]

    return _EXPRESSION.sub(lookup, text)


def _python3_shim(repo: Path) -> Path:
    shim_dir = repo / ".shim"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim_dir


def _run_step(
    step_id: str, values: dict[str, str], repo: Path, env: dict[str, str] | None = None
) -> dict[str, str]:
    step = _steps()[step_id]
    script = _render(str(step["run"]), values)
    output = repo / ".github_output"
    output.write_text("", encoding="utf-8")
    step_env = {
        key: _render(str(val), values) for key, val in (step.get("env") or {}).items()
    }
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "PATH": f"{_python3_shim(repo)}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(repo),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_ACTION_PATH": str(ACTION_DIR),
            "GITHUB_WORKSPACE": str(repo),
            **step_env,
            **(env or {}),
        },
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


@pytest.fixture
def released_head(tmp_path: Path) -> Path:
    """A repo on main whose HEAD commit carries the release trailer."""

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path),
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI")
    (tmp_path / "README").write_text("x\n", encoding="utf-8")
    git("add", "README")
    git("commit", "-q", "-m", "fix: a thing\n\nRelease: true")
    return tmp_path


def _gate(event: str, repo: Path) -> dict[str, str]:
    return _run_step(
        "gate",
        {
            "github.event_name": event,
            "github.ref": "refs/heads/main",
            "inputs.tag": "",
            "inputs.from-head": "",
        },
        repo,
    )


def _derive(event: str, will_publish: str, repo: Path) -> dict[str, str]:
    return _run_step(
        "derive",
        {
            "steps.gate.outputs.will-publish": will_publish,
            "github.event_name": event,
            "inputs.branch-build": "",
            "github.ref": "refs/heads/main",
            "steps.worthy.outputs.release-worthy": "",
            "steps.predict.outputs.version || steps.forced.outputs.version": "",
        },
        repo,
    )


def _tier(event: str, will_publish: str, requested: str, repo: Path) -> dict[str, str]:
    return _run_step(
        "tier",
        {
            "github.event_name": event,
            "steps.gate.outputs.will-publish": will_publish,
            "inputs.test-tier": requested,
        },
        repo,
    )


def test_the_steps_python3_is_this_interpreter(tmp_path: Path) -> None:
    result = subprocess.run(
        ["python3", "-c", "import sys, yaml; print(sys.executable)"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "PATH": f"{_python3_shim(tmp_path)}{os.pathsep}{os.environ.get('PATH', '')}"
        },
        check=True,
    )
    assert result.stdout.strip() == sys.executable


class TestAScheduledRun:
    """A schedule tests at full and never builds or publishes."""

    def test_a_push_with_the_trailer_publishes(self, released_head: Path) -> None:
        # The control: this repo WOULD publish on a push, so the schedule
        # case below is proving something.
        assert _gate("push", released_head)["will-publish"] == "true"

    def test_a_schedule_never_publishes_whatever_the_trailer(
        self, released_head: Path
    ) -> None:
        assert _gate("schedule", released_head)["will-publish"] == "false"

    def test_a_schedule_runs_checks_and_no_build(self, released_head: Path) -> None:
        outputs = _derive("schedule", "false", released_head)
        assert outputs["run-checks"] == "true"
        assert outputs["run-build"] == "false"
        assert outputs["run-arm64-check"] == "false"

    def test_a_schedule_runs_full(self, released_head: Path) -> None:
        assert _tier("schedule", "false", "core", released_head)["test-tier"] == "full"


class TestTheOtherEvents:
    def test_a_pr_runs_core(self, released_head: Path) -> None:
        assert (
            _tier("pull_request", "false", "core", released_head)["test-tier"] == "core"
        )

    def test_a_release_runs_core_until_the_repo_opts_in(
        self, released_head: Path
    ) -> None:
        outputs = _tier("push", "true", "core", released_head)
        assert outputs["test-tier"] == "core"
        assert outputs["full-required-for-release"] == "false"

    def test_an_opted_in_release_runs_full(self, released_head: Path) -> None:
        (released_head / ".hyperi-ci.yaml").write_text(
            "test:\n  full:\n    required_for_release: true\n", encoding="utf-8"
        )
        outputs = _tier("push", "true", "core", released_head)
        assert outputs["test-tier"] == "full"
        assert outputs["full-required-for-release"] == "true"

    def test_a_dispatch_asking_for_full_runs_full(self, released_head: Path) -> None:
        outputs = _tier("workflow_dispatch", "false", "full", released_head)
        assert outputs["test-tier"] == "full"

    def test_a_non_worthy_push_still_skips_the_checks(
        self, released_head: Path
    ) -> None:
        # The schedule clause must not widen run-checks for anything else.
        outputs = _derive("push", "false", released_head)
        assert outputs["run-checks"] == "false"
