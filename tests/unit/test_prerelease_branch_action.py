# Project:   HyperI CI
# File:      tests/unit/test_prerelease_branch_action.py
# Purpose:   Tests for the predict-version prerelease-branch gate helper
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `.github/actions/predict-version/prerelease_branch.py` (issue #144).

Run as a real subprocess against a real workspace, because what is under test
is the by-path package load the composite performs on a runner where hyperi-ci
is not installed. Importing the module here would skip exactly that.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "actions" / "predict-version" / "prerelease_branch.py"


def _run(workspace: Path, ref: str) -> str:
    env = dict(os.environ)
    env["GITHUB_WORKSPACE"] = str(workspace)
    env["GITHUB_REF"] = ref
    # An installed hyperi-ci would mask a broken by-path load, which is the
    # thing under test.
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        cwd=workspace,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


class TestAgainstTheCentralConfig:
    def test_beta_is_a_release_branch(self, workspace: Path) -> None:
        assert _run(workspace, "refs/heads/beta") == "true"

    def test_main_is_not(self, workspace: Path) -> None:
        # main releases, but through the gate's own stable path -- this helper
        # answers only the prerelease question.
        assert _run(workspace, "refs/heads/main") == "false"

    def test_a_feature_branch_is_not(self, workspace: Path) -> None:
        assert _run(workspace, "refs/heads/fix/144-something") == "false"

    def test_an_empty_ref_is_not(self, workspace: Path) -> None:
        assert _run(workspace, "") == "false"


class TestAgainstARepoConfig:
    def test_a_repo_config_decides_for_itself(self, workspace: Path) -> None:
        (workspace / ".releaserc.json").write_text(
            json.dumps(
                {
                    "branches": ["main", {"name": "next", "prerelease": True}],
                    "plugins": ["@semantic-release/exec"],
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        assert _run(workspace, "refs/heads/next") == "true"
        assert _run(workspace, "refs/heads/beta") == "false"

    def test_a_config_naming_the_37_plugins_falls_back_to_central(
        self, workspace: Path
    ) -> None:
        (workspace / ".releaserc.json").write_text(
            json.dumps(
                {
                    "branches": ["main", {"name": "next", "prerelease": True}],
                    "plugins": ["@semantic-release/git"],
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        assert _run(workspace, "refs/heads/beta") == "true"
        assert _run(workspace, "refs/heads/next") == "false"
