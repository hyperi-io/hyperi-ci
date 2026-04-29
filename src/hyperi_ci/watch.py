# Project:   HyperI CI
# File:      src/hyperi_ci/watch.py
# Purpose:   Watch a GitHub Actions run to completion
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Watch GitHub Actions runs to completion.

Polls a workflow run with exponential backoff until it reaches a terminal
status, then reports the result with job-level detail.

Tier 2 (PGO + BOLT) Rust builds for both archs in parallel can take
35-45 min, so the default timeout is set generously (60 min). For longer
workflows pass `--timeout 0` to disable timeout entirely; the watcher
will keep polling until the run reaches a terminal state.

When a timeout *is* hit while a run is still in progress, the report
includes the current status + a copy-pasteable resume command, so the
caller knows whether to re-watch or investigate.
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

# After this many consecutive `gh run view` failures, consider the
# remote unreachable and exit with an error rather than spinning
# forever. Each failure is followed by a (capped exponential) backoff,
# so 10 covers ~6 minutes of sustained outage before giving up.
_MAX_CONSECUTIVE_FETCH_FAILURES = 10

# Default timeout in seconds. Sized to cover Tier 2 (PGO + BOLT) Rust
# builds for both archs in parallel, which routinely take 35-45 min.
# Pass 0 (`--timeout 0` on the CLI) to disable timeout entirely.
_DEFAULT_TIMEOUT = 3600


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
        Dict with status/conclusion/jobs, or None on transient error.

    Note: returns None on both subprocess and JSON parse errors. The
    caller treats None as "transient — retry"; only after multiple
    consecutive failures should it be considered fatal. See
    `_MAX_CONSECUTIVE_FETCH_FAILURES`.
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


def _resume_command(run_id: str, timeout: int) -> str:
    """Format a copy-pasteable resume command for the user."""
    if timeout == 0:
        return f"hyperi-ci watch {run_id} --timeout 0"
    return f"hyperi-ci watch {run_id} --timeout {timeout}"


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
    timeout: int = _DEFAULT_TIMEOUT,
    interval: int = 30,
) -> int:
    """Watch a GitHub Actions run to completion.

    Args:
        run_id: Run ID to watch. Auto-detects latest on current branch if None.
        timeout: Maximum seconds to wait. Pass `0` to disable timeout
            (poll until the run reaches a terminal state). Default is
            sized for Tier 2 Rust builds (3600 s = 60 min).
        interval: Base poll interval in seconds.

    Returns:
        Exit code: 0=success, 1=failed/cancelled/unreachable, 2=timeout.
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

    if timeout == 0:
        info(f"Watching run {run_id} (no timeout)")
    else:
        info(f"Watching run {run_id} (timeout: {timeout}s)")

    # `deadline = None` disables the timeout check entirely.
    deadline: float | None = None if timeout == 0 else time.monotonic() + timeout
    attempt = 0
    consecutive_failures = 0
    last_known_status = "unknown"

    while deadline is None or time.monotonic() < deadline:
        attempt += 1
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        info(f"  [{now}] polling (attempt {attempt})...")

        run_data = _get_run_status(run_id)
        if not run_data:
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_FETCH_FAILURES:
                error(
                    f"  Failed to fetch run status "
                    f"{consecutive_failures} times in a row — giving up. "
                    f"Last known status: {last_known_status}. "
                    f"Resume: {_resume_command(run_id, timeout)}"
                )
                return 1
            warn(
                f"  Failed to fetch run status "
                f"({consecutive_failures}/{_MAX_CONSECUTIVE_FETCH_FAILURES}) "
                f"— retrying"
            )
            time.sleep(_poll_interval(interval, attempt))
            continue

        # Recover from prior transient failures.
        consecutive_failures = 0

        status = run_data.get("status", "unknown")
        last_known_status = status
        if status in _TERMINAL_STATUSES:
            _print_summary(run_data)
            conclusion = run_data.get("conclusion", "unknown")
            if conclusion == "success":
                return 0
            return 1

        info(f"  status: {status}")
        wait = _poll_interval(interval, attempt)
        time.sleep(wait)

    # Timed out. Report the most recent known status + a copy-pasteable
    # resume command so the caller can decide whether to re-watch (still
    # in progress) or investigate (stuck / silently failing).
    error(
        f"Timeout after {timeout} seconds — run still {last_known_status}. "
        f"Resume: {_resume_command(run_id, timeout)} "
        f"(or use --timeout 0 to disable timeout)"
    )
    return 2
