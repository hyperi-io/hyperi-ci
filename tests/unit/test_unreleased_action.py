# Project:   HyperI CI
# File:      tests/unit/test_unreleased_action.py
# Purpose:   Tests for the predict-version unreleased-work warning helper
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `.github/actions/predict-version/unreleased.py`.

Run as a real subprocess against a real git repo, because what is under test
is the by-path package load the composite performs on a runner where hyperi-ci
is not installed. Importing the module here would skip exactly that.

The contract the gate consumes: stdout is the warning text, and EMPTY when
there is nothing to warn about.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "actions" / "predict-version" / "unreleased.py"


def _run(cwd: Path, script: Path = SCRIPT) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # An installed hyperi-ci would mask a broken by-path load, which is the
    # thing under test.
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        cwd=cwd,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.io")
    _git(path, "config", "user.name", "t")
    return path


def _commit(cwd: Path, msg: str) -> None:
    _git(cwd, "commit", "--allow-empty", "-q", "-m", msg)


class TestTheGateContract:
    def test_work_waiting_prints_the_warning_text(self, repo: Path) -> None:
        _commit(repo, "chore: seed")
        _git(repo, "tag", "v2.12.3")
        _commit(repo, "fix(deps): raise floors off three live advisories")
        _commit(repo, "fix: an off-by-one")

        result = _run(repo)
        assert result.returncode == 0, result.stderr
        assert "2 releasable commits sit unreleased since v2.12.3" in result.stdout
        assert "hyperi-ci push --publish" in result.stdout

    def test_nothing_waiting_prints_nothing_to_stdout(self, repo: Path) -> None:
        _commit(repo, "chore: seed")
        _git(repo, "tag", "v2.12.3")
        _commit(repo, "docs: a typo")

        result = _run(repo)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""
        assert "nothing releasable waiting since v2.12.3" in result.stderr

    def test_no_baseline_is_reported_separately(self, repo: Path) -> None:
        _commit(repo, "fix: the very first commit")

        result = _run(repo)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""
        assert "no released baseline" in result.stderr


class TestItFailsOpen:
    """It decides only whether a warning appears, so it never fails the job."""

    def test_outside_a_git_repo(self, tmp_path: Path) -> None:
        result = _run(tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_when_the_package_is_not_beside_it(self, tmp_path: Path) -> None:
        # The by-path load is the fragile part: a moved action directory
        # leaves `src/hyperi_ci` unreachable. That must go quiet, not take
        # the release gate down with it.
        stranded = tmp_path / ".github" / "actions" / "predict-version"
        stranded.mkdir(parents=True)
        shutil.copy(SCRIPT, stranded / SCRIPT.name)

        result = _run(tmp_path, stranded / SCRIPT.name)
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert "unreleased-work check failed" in result.stderr
