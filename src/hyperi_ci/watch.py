# Project:   HyperI CI
# File:      src/hyperi_ci/watch.py
# Purpose:   Watch a GitHub Actions run to completion
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Watch GitHub Actions runs to completion.

Polls a workflow run with exponential backoff until it reaches a terminal
status, then reports the result with job-level detail.

Pinned selection (issue #101): with no run id, the run is resolved from
the commit at HEAD and the workflow the project declares in its ci.yml,
``--workflow`` names any other, and the watch refuses when several runs
still match. The old "newest run on the branch" lookup reported green
off a Dependency Graph run while the Test run for the same commit was
still going, and off the previous commit's run in the seconds before the
new one registered.

Early-fail-on-red (issue #58): the poll exits non-zero the instant ANY
job concludes failure/cancelled/timed_out, rather than waiting for the
whole run to finish. A fleet watcher polling N runs in sequence must not
block for the remaining ~hour on a run that is already doomed.

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
from hyperi_ci.gate_audit import NO_VERDICT, gate_of
from hyperi_ci.gh import (
    RunSelectionError,
    describe_run,
    gh_run,
    head_run_candidates,
    require_gh,
    select_run_for_head,
)

_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
    }
)

# A single job reaching one of these conclusions dooms the whole run, so
# fail fast (issue #58) instead of waiting for the run's own terminal
# status - which for a big multi-arch PGO+BOLT build can be tens of
# minutes away. Job conclusions are populated the moment each job ends.
_FAILED_JOB_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out"})

# After this many consecutive `gh run view` failures, consider the
# remote unreachable and exit with an error rather than spinning
# forever. Each failure is followed by a (capped exponential) backoff,
# so 10 covers ~6 minutes of sustained outage before giving up.
_MAX_CONSECUTIVE_FETCH_FAILURES = 10

# Default timeout in seconds. Sized to cover Tier 2 (PGO + BOLT) Rust
# builds for both archs in parallel, which routinely take 35-45 min.
# Pass 0 (`--timeout 0` on the CLI) to disable timeout entirely.
_DEFAULT_TIMEOUT = 3600

# GitHub registers a run seconds after the push, so wait for HEAD's own
# run rather than watching the previous commit's.
_RUN_APPEAR_TIMEOUT = 90
_RUN_APPEAR_POLL = 5.0

# Headroom over the handful of runs one commit produces.
_RUN_LIST_LIMIT = 30


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


def _get_run_status(run_id: str, repo: str | None = None) -> dict | None:
    """Fetch current run status.

    Args:
        run_id: Workflow run ID.
        repo: Optional ``owner/name`` — pass this when watching a run
            in a different repo than the current working directory.
            ``gh run view`` defaults to the cwd's git remote and
            silently 404s when the run isn't there, which the watch
            loop misreads as transient network failure.

    Returns:
        Dict with status/conclusion/jobs, or None on transient error.

    Note: returns None on both subprocess and JSON parse errors. The
    caller treats None as "transient — retry"; only after multiple
    consecutive failures should it be considered fatal. See
    `_MAX_CONSECUTIVE_FETCH_FAILURES`.

    """
    args = [
        "run",
        "view",
        run_id,
        "--json",
        "status,conclusion,jobs,url,workflowName,headBranch",
    ]
    if repo:
        args.extend(["--repo", repo])
    try:
        result = gh_run(args)
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _first_failed_job(run_data: dict) -> dict | None:
    """Return the first job in a failure conclusion, or None.

    A job's ``conclusion`` is populated as soon as that job finishes,
    well before the overall run ``status`` flips to a terminal value.
    Scanning job conclusions each tick is what lets watch fail fast
    (issue #58) rather than blocking to the end of a doomed run.

    Caveat: a job with ``continue-on-error: true`` can conclude
    ``failure`` while the run overall still concludes ``success``, so
    this would early-fail such a run. No hyperi-ci / consumer workflow
    uses job-level continue-on-error today; revisit if that changes.
    """
    for job in run_data.get("jobs", []):
        if job.get("conclusion") in _FAILED_JOB_CONCLUSIONS:
            return job
    return None


def _resume_command(run_id: str, timeout: int, repo: str | None = None) -> str:
    """Format a copy-pasteable resume command for the user."""
    repo_arg = f" --repo {repo}" if repo else ""
    if timeout == 0:
        return f"hyperi-ci watch {run_id}{repo_arg} --timeout 0"
    return f"hyperi-ci watch {run_id}{repo_arg} --timeout {timeout}"


def _gates_unrun(run_data: dict) -> bool:
    """Return True when the run has gate jobs and none of them reached a verdict.

    Shares `gate_of` and `NO_VERDICT` with the gate audit so the two reports
    cannot disagree about what counts as a gate having answered.
    """
    gates = [j for j in run_data.get("jobs", []) if gate_of(j.get("name", ""))]
    if not gates:
        return False
    return all(job.get("conclusion") in NO_VERDICT for job in gates)


def job_lines(jobs: list[dict]) -> list[tuple[str, str]]:
    """Render the per-job summary lines as ``(level, text)`` pairs.

    Jobs that reached a verdict come first; skipped jobs follow under their
    own count, so a skipped job is never read as one of the passes above it.

    Args:
        jobs: The ``jobs`` list from ``gh run view --json jobs``.

    Returns:
        Pairs whose level is ``success``, ``error``, ``warn`` or ``info``.

    """
    lines: list[tuple[str, str]] = []
    skipped: list[str] = []
    for job in jobs:
        name = job.get("name", "unknown")
        job_conclusion = job.get("conclusion") or "pending"
        if job_conclusion == "skipped":
            skipped.append(name)
            continue
        marker = "pass" if job_conclusion == "success" else job_conclusion
        line = f"  {marker}: {name}"
        if job_conclusion == "success":
            lines.append(("success", line))
        elif job_conclusion == "failure":
            lines.append(("error", line))
            for step_data in job.get("steps", []):
                if step_data.get("conclusion") == "failure":
                    step = step_data.get("name", "unknown")
                    lines.append(("error", f"    failed step: {step}"))
        else:
            lines.append(("info", line))

    if skipped:
        lines.append(("info", f"  did not run ({len(skipped)}):"))
        for name in skipped:
            # Rendered neutral, a skipped gate reads as one that passed.
            level = "warn" if gate_of(name) else "info"
            lines.append((level, f"    skipped: {name}"))
    return lines


def _print_summary(run_data: dict) -> None:
    """Print a human-readable run summary with job statuses."""
    conclusion = run_data.get("conclusion", "unknown")
    workflow = run_data.get("workflowName", "unknown")
    branch = run_data.get("headBranch", "unknown")
    url = run_data.get("url", "")

    header = f"{workflow} on {branch}: {conclusion}"
    if conclusion == "success" and _gates_unrun(run_data):
        # Green over a gate that never ran is the lie in issue #96, and the
        # watcher is where a human reads the verdict.
        warn(f"{header} -- quality + test did NOT run; nothing was verified")
    elif conclusion == "success":
        success(header)
    elif conclusion in ("failure", "cancelled"):
        error(header)
    else:
        warn(header)

    emit = {"success": success, "error": error, "warn": warn, "info": info}
    for level, text in job_lines(run_data.get("jobs", [])):
        emit[level](text)

    if url:
        info(f"  {url}")


def resolve_head_run(*, workflow: str | None, repo: str | None) -> dict:
    """Resolve the run for the commit at HEAD, waiting for it to register.

    Args:
        workflow: Workflow name to narrow on. With none, the project's
            declared CI workflow is the pin.
        repo: Optional ``owner/name`` - rejected here, since HEAD says
            nothing about another repo's runs.

    Returns:
        The single run matching HEAD (and the workflow, when given).

    Raises:
        RunSelectionError: nothing registered inside the appearance
            budget, or several runs match and the choice is ambiguous.

    """
    deadline = time.monotonic() + _RUN_APPEAR_TIMEOUT
    while True:
        head_sha, runs = head_run_candidates(repo=repo, limit=_RUN_LIST_LIMIT)
        if runs:
            return select_run_for_head(runs, head_sha=head_sha, workflow=workflow)
        if time.monotonic() >= deadline:
            raise RunSelectionError(
                f"No run registered for commit {head_sha[:8]} after "
                f"{_RUN_APPEAR_TIMEOUT}s - has it been pushed?"
            )
        info(f"  no run registered for {head_sha[:8]} yet - waiting...")
        time.sleep(_RUN_APPEAR_POLL)


def watch_run(
    *,
    run_id: str | None = None,
    workflow: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    interval: int = 30,
    repo: str | None = None,
) -> int:
    """Watch a GitHub Actions run to completion.

    Args:
        run_id: Run ID to watch. With none, the run built from the commit
            at HEAD is used, and an ambiguous choice is refused rather
            than guessed (issue #101).
        workflow: Workflow name to pin on, matched case-insensitively,
            exact before substring. Defaults to the name declared in the
            project's ci.yml. Ignored when a run id is given.
        timeout: Maximum seconds to wait. Pass `0` to disable timeout
            (poll until the run reaches a terminal state). Default is
            sized for Tier 2 Rust builds (3600 s = 60 min).
        interval: Base poll interval in seconds.
        repo: Optional ``owner/name`` — when set, all gh calls target
            this repo instead of the cwd's git remote. Use this when
            watching a run in a different repo than your cwd; it needs
            an explicit run id.

    Returns:
        Exit code: 0=success, 1=failed/cancelled/unreachable, 2=timeout.

    """
    if not require_gh():
        return 1

    if not run_id:
        try:
            run = resolve_head_run(workflow=workflow, repo=repo)
        except RunSelectionError as exc:
            error(str(exc))
            return 1
        run_id = str(run["databaseId"])
        info(f"Pinned to run {describe_run(run)}")

    repo_label = f" in {repo}" if repo else ""
    if timeout == 0:
        info(f"Watching run {run_id}{repo_label} (no timeout)")
    else:
        info(f"Watching run {run_id}{repo_label} (timeout: {timeout}s)")

    # `deadline = None` disables the timeout check entirely.
    deadline: float | None = None if timeout == 0 else time.monotonic() + timeout
    attempt = 0
    consecutive_failures = 0
    last_known_status = "unknown"

    while deadline is None or time.monotonic() < deadline:
        attempt += 1
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        info(f"  [{now}] polling (attempt {attempt})...")

        run_data = _get_run_status(run_id, repo=repo)
        if not run_data:
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_FETCH_FAILURES:
                error(
                    f"  Failed to fetch run status "
                    f"{consecutive_failures} times in a row — giving up. "
                    f"Last known status: {last_known_status}. "
                    f"Resume: {_resume_command(run_id, timeout, repo=repo)}"
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

        # Early-fail-on-red (issue #58): the instant ANY job has concluded
        # failure/cancelled/timed_out, stop - do not wait for the whole run
        # to reach a terminal status. Returns 1 (a job went red), matching
        # the failed-run terminal path below.
        failed_job = _first_failed_job(run_data)
        if failed_job:
            _print_summary(run_data)
            error(
                f"  Early-fail: job '{failed_job.get('name', 'unknown')}' "
                f"concluded '{failed_job.get('conclusion')}' - not waiting "
                f"for the rest of the run. {run_data.get('url', '')}".rstrip()
            )
            return 1

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
        f"Resume: {_resume_command(run_id, timeout, repo=repo)} "
        f"(or use --timeout 0 to disable timeout)"
    )
    return 2
