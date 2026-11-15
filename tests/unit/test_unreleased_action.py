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

The contract the gate consumes: stdout is the whole annotation --
``::warning::`` once the oldest waiting commit is older than
``release.unreleased_warn_days``, ``::notice::`` before that -- and EMPTY when
there is nothing waiting.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.git_helpers import commit_at, git, init_repo

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "actions" / "predict-version" / "unreleased.py"


def _run(cwd: Path, script: Path = SCRIPT) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # An installed hyperi-ci would mask a broken by-path load, which is the
    # thing under test.
    env.pop("PYTHONPATH", None)
    env["GITHUB_WORKSPACE"] = str(cwd)
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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def _set_warn_days(repo: Path, value: str) -> None:
    (repo / ".hyperi-ci.yaml").write_text(
        f"release:\n  unreleased_warn_days: {value}\n", encoding="utf-8", newline="\n"
    )


class TestTheGateContract:
    def test_old_work_warns_with_the_pinned_text(self, repo: Path) -> None:
        # Byte for byte what the gate raised before the age gate existed. A
        # consumer keys its findings on it.
        commit_at(repo, "chore: seed", days_ago=30)
        git(repo, "tag", "v2.12.3")
        commit_at(repo, "fix(deps): raise floors off three live advisories", 20)
        commit_at(repo, "fix: an off-by-one", 20)

        result = _run(repo)
        assert result.returncode == 0, result.stderr
        assert result.stdout == (
            "::warning::2 releasable commits sit unreleased since v2.12.3, tagged "
            "30 days ago (2 patch). This run validated them and published nothing. "
            "Ship them with 'hyperi-ci push --publish', or re-run this workflow "
            "with from-head=true.\n"
        )
        assert "release.unreleased_warn_days=7 from defaults.yaml" in result.stderr

    def test_young_work_is_a_notice_with_the_same_text(self, repo: Path) -> None:
        commit_at(repo, "chore: seed", days_ago=30)
        git(repo, "tag", "v2.12.3")
        commit_at(repo, "fix: an off-by-one")

        result = _run(repo)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith(
            "::notice::1 releasable commit sits unreleased since v2.12.3"
        )

    def test_the_project_sets_the_threshold(self, repo: Path) -> None:
        _set_warn_days(repo, "0")
        commit_at(repo, "chore: seed", days_ago=30)
        git(repo, "tag", "v2.12.3")
        commit_at(repo, "fix: an off-by-one")

        result = _run(repo)
        assert result.stdout.startswith("::warning::1 releasable commit sits")
        assert "release.unreleased_warn_days=0 from .hyperi-ci.yaml" in result.stderr

    def test_a_threshold_that_is_not_days_warns_on_both(self, repo: Path) -> None:
        _set_warn_days(repo, "soon")
        commit_at(repo, "chore: seed", days_ago=30)
        git(repo, "tag", "v2.12.3")
        commit_at(repo, "fix: an off-by-one")

        lines = _run(repo).stdout.splitlines()
        assert lines[0].startswith("::warning title=release.unreleased_warn_days::")
        assert lines[1].startswith("::warning::1 releasable commit sits")

    def test_nothing_waiting_prints_nothing_to_stdout(self, repo: Path) -> None:
        commit_at(repo, "chore: seed")
        git(repo, "tag", "v2.12.3")
        commit_at(repo, "docs: a typo")

        result = _run(repo)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""
        assert "nothing releasable waiting since v2.12.3" in result.stderr

    def test_no_baseline_is_reported_separately(self, repo: Path) -> None:
        commit_at(repo, "fix: the very first commit")

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
