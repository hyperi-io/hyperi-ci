# Project:   HyperI CI
# File:      tests/git_helpers.py
# Purpose:   One git runner and repo builder for tests that need a real repository
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import time
from pathlib import Path

from hyperi_ci.common import run_cmd


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run ``git`` in ``cwd`` and return its stripped stdout. A failure raises."""
    return run_cmd(["git", *args], cwd=cwd, env=env, capture=True).stdout.strip()


def init_repo(path: Path, *, branch: str | None = None, seed: bool = False) -> Path:
    """Initialise a repository at ``path`` with a committer identity.

    ``seed`` adds one empty ``chore: seed`` commit.
    """
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", *(["-b", branch] if branch else []))
    git(path, "config", "user.email", "t@t.io")
    git(path, "config", "user.name", "t")
    if seed:
        commit(path, "chore: seed")
    return path


def commit(cwd: Path, message: str, *, env: dict[str, str] | None = None) -> str:
    """Make an empty commit with ``message`` and return its SHA."""
    git(cwd, "commit", "--allow-empty", "-q", "-m", message, env=env)
    return git(cwd, "rev-parse", "HEAD")


def commit_at(cwd: Path, message: str, days_ago: int = 0) -> str:
    """Commit dated ``days_ago``, so an age a message quotes is deterministic."""
    stamp = f"{int(time.time()) - days_ago * 86400} +0000"
    return commit(
        cwd, message, env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    )
