# Project:   HyperI CI
# File:      src/hyperi_ci/watch.py
# Purpose:   Watch a GitHub Actions run to completion
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Watch GitHub Actions runs to completion.

Polls a workflow run with exponential backoff until it reaches a terminal
status, then reports the result with job-level detail.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.gh import get_current_branch, get_latest_run, gh_run, require_gh

_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
    }
)


def _poll_interval(base: int, attempt: int) -> float:
    """Calculate poll interval with exponential backoff.

    Caps at 120 seconds regardless of attempt count.

    Args:
        base: Base interval in seconds.
        attempt: Current attempt number (1-based).

    Returns:
        Seconds to wait before next poll.
    """
    return min(base * (1.5 ** min(attempt - 1, 4)), 120.0)


def _get_run_status(run_id: str) -> dict | None:
    """Fetch current run status.

    Args:
        run_id: Workflow run ID.

    Returns:
        Dict with status/conclusion/jobs, or None on error.
    """
    try:
        result = gh_run(
            [
                "run",
                "view",
                run_id,
                "--json",
                "status,conclusion,jobs,url,workflowName,headBranch",
            ]
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _print_summary(run_data: dict) -> None:
    """Print a human-readable run summary with job statuses."""
    conclusion = run_data.get("conclusion", "unknown")
    workflow = run_data.get("workflowName", "unknown")
    branch = run_data.get("headBranch", "unknown")
    url = run_data.get("url", "")

    header = f"{workflow} on {branch}: {conclusion}"
    if conclusion == "success":
        success(header)
    elif conclusion in ("failure", "cancelled"):
        error(header)
    else:
        warn(header)

    jobs = run_data.get("jobs", [])
    for job in jobs:
        name = job.get("name", "unknown")
        job_conclusion = job.get("conclusion", "pending")
        marker = "pass" if job_conclusion == "success" else job_conclusion
        line = f"  {marker}: {name}"

        if job_conclusion == "success":
            success(line)
        elif job_conclusion == "failure":
            error(line)
            steps = job.get("steps", [])
            for step_data in steps:
                if step_data.get("conclusion") == "failure":
                    error(f"    failed step: {step_data.get('name', 'unknown')}")
        else:
            info(line)

    if url:
        info(f"  {url}")


def watch_run(
    *,
    run_id: str | None = None,
    timeout: int = 1800,
    interval: int = 30,
) -> int:
    """Watch a GitHub Actions run to completion.

    Args:
        run_id: Run ID to watch. Auto-detects latest on current branch if None.
        timeout: Maximum seconds to wait.
        interval: Base poll interval in seconds.

    Returns:
        Exit code: 0=success, 1=failed/cancelled, 2=timeout.
    """
    if not require_gh():
        return 1

    if not run_id:
        branch = get_current_branch()
        if not branch:
            error("Could not detect branch — provide a run ID")
            return 1

        info(f"Finding latest run on {branch}...")
        latest = get_latest_run(branch=branch)
        if not latest:
            error(f"No runs found on {branch}")
            return 1
        run_id = str(latest["databaseId"])

    info(f"Watching run {run_id}")

    deadline = time.monotonic() + timeout
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        info(f"  [{now}] polling (attempt {attempt})...")

        run_data = _get_run_status(run_id)
        if not run_data:
            warn("  Failed to fetch run status — retrying")
            time.sleep(_poll_interval(interval, attempt))
            continue

        status = run_data.get("status", "unknown")
        if status in _TERMINAL_STATUSES:
            _print_summary(run_data)
            conclusion = run_data.get("conclusion", "unknown")
            if conclusion == "success":
                return 0
            return 1

        info(f"  status: {status}")
        wait = _poll_interval(interval, attempt)
        time.sleep(wait)

    error(f"Timeout after {timeout} seconds")
    return 2
