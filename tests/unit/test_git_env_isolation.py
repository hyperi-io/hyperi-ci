# Project:   HyperI CI
# File:      tests/unit/test_git_env_isolation.py
# Purpose:   A test never inherits the git environment of the repo running it
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A test never inherits the git environment of the repo running the suite.

`git rebase --exec` and git hooks export GIT_DIR. A test that then runs
`git init <tmp>` re-initialises the calling repository rather than creating
one, and a linked worktree's GIT_DIR sets `core.bare=true` on the main
checkout. The autouse fixture in conftest.py clears those variables; these
tests fail when the suite runs under an exported GIT_DIR without it.
"""

import os
import shutil

import pytest

from hyperi_ci.common import run_cmd

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _repo_local_names() -> list[str]:
    result = run_cmd(["git", "rev-parse", "--local-env-vars"], capture=True)
    return result.stdout.split()


def test_git_names_the_variables_that_bind_it_to_a_repo() -> None:
    binding = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"}
    assert binding <= set(_repo_local_names())


def test_no_repo_local_git_variable_reaches_a_test() -> None:
    leaked = [name for name in _repo_local_names() if name in os.environ]
    assert not leaked, f"a test inherited {leaked}; git calls here hit the calling repo"
