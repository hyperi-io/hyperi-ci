# Project:   HyperI CI
# File:      src/hyperi_ci/trigger.py
# Purpose:   Trigger a GitHub Actions workflow run
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Trigger GitHub Actions workflow runs.

Dispatches a workflow_dispatch event via the gh CLI, waits for the run
to appear, and optionally watches it to completion.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.gh import get_current_branch, get_latest_run, gh_run, require_gh


def _wait_for_run(
    branch: str,
    workflow: str,
    before_time: float,
    max_wait: int = 60,
) -> str | None:
    """Wait for a new run to appear after triggering.

    Polls every 2 seconds for up to max_wait seconds, looking for a run
    that was created after before_time.

    Args:
        branch: Branch the run should be on.
        workflow: Workflow filename.
        before_time: Unix timestamp before the trigger was sent.
        max_wait: Maximum seconds to wait.

    Returns:
        Run ID as string, or None if no run appeared.

    """
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        time.sleep(2)
        run = get_latest_run(branch=branch, workflow=workflow)
        if not (run and run.get("databaseId")):
            continue
        # Filter out the previous run still showing as "latest" — gh's
        # listing isn't strictly ordered by trigger time, and we need the
        # NEW run, not whatever stale one happens to come back first.
        created = run.get("createdAt")
        if created:
            try:
                created_ts = datetime.fromisoformat(
                    created.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                created_ts = 0.0
            if created_ts < before_time:
                continue
        return str(run["databaseId"])
    return None


def trigger_workflow(
    *,
    workflow: str = "ci.yml",
    ref: str | None = None,
    watch: bool = False,
    timeout: int = 1800,
    interval: int = 30,
) -> int:
    """Trigger a GitHub Actions workflow run.

    Args:
        workflow: Workflow filename (e.g. "ci.yml").
        ref: Branch or tag to run on. Defaults to current branch.
        watch: If True, watch the run to completion after triggering.
        timeout: Watch timeout in seconds.
        interval: Watch poll interval in seconds.

    Returns:
        Exit code: 0=success, 1=failed, 2=timeout.

    """
    if not require_gh():
        return 1

    branch = ref or get_current_branch()
    if not branch:
        error("Could not detect current branch — use --ref to specify")
        return 1

    info(f"Triggering {workflow} on {branch}")

    try:
        gh_run(
            ["workflow", "run", workflow, "--ref", branch],
            capture=False,
            check=True,
        )
    except subprocess.CalledProcessError:
        error(f"Failed to trigger workflow {workflow}")
        return 1

    success(f"Triggered {workflow} on {branch}")

    if not watch:
        return 0

    info("Waiting for run to appear...")
    before = time.time()
    run_id = _wait_for_run(branch, workflow, before)
    if not run_id:
        warn("Run did not appear within 60 seconds")
        return 2

    info(f"Run {run_id} started — watching...")

    from hyperi_ci.watch import watch_run

    return watch_run(run_id=run_id, timeout=timeout, interval=interval)
