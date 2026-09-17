# Project:   HyperI CI
# File:      src/hyperi_ci/rerun.py
# Purpose:   Re-run a GitHub Actions run, failed jobs only by default
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Re-run a GitHub Actions run.

`gh run rerun --failed` had no wrapper, so telling a flake from a real
failure meant reaching for the native CLI or pushing an empty commit
(issue #97). Run selection matches `watch`: with no run id, the run built
from the commit at HEAD, and an ambiguous choice refused rather than
guessed.
"""

from __future__ import annotations

import subprocess

from hyperi_ci.common import error, info, success
from hyperi_ci.gh import RunSelectionError, describe_run, gh_run, require_gh
from hyperi_ci.watch import resolve_head_run


def rerun_run(
    *,
    run_id: str | None = None,
    workflow: str | None = None,
    repo: str | None = None,
    failed_only: bool = True,
) -> int:
    """Re-run a run, or only the jobs in it that failed.

    Args:
        run_id: Run to re-run. With none, the run built from the commit at
            HEAD is used.
        workflow: Workflow name to pin on when resolving from HEAD.
            Ignored when a run id is given.
        repo: Optional ``owner/name``. Needs an explicit run id, since HEAD
            says nothing about another repo's runs.
        failed_only: Re-run only the failed jobs and what depends on them.
            False re-runs every job in the run.

    Returns:
        Exit code: 0=dispatched, 1=refused or the dispatch failed.

    """
    if not require_gh():
        return 1

    if not run_id:
        if repo:
            error(
                "--repo needs an explicit run id — HEAD says nothing about "
                "which run in another repo you mean"
            )
            return 1
        try:
            run = resolve_head_run(workflow=workflow, repo=None)
        except RunSelectionError as exc:
            error(str(exc))
            return 1
        run_id = str(run["databaseId"])
        info(f"Pinned to run {describe_run(run)}")

    cmd = ["run", "rerun", run_id]
    if failed_only:
        cmd.append("--failed")
    if repo:
        cmd.extend(["--repo", repo])

    scope = "failed jobs" if failed_only else "all jobs"
    info(f"Re-running {scope} in run {run_id}")

    try:
        gh_run(cmd, capture=False, check=True)
    except subprocess.CalledProcessError:
        error(f"Failed to re-run {run_id}")
        return 1

    success(f"Re-run dispatched for run {run_id}")
    return 0
