# Project:   HyperI CI
# File:      src/hyperi_ci/logs.py
# Purpose:   Fetch and filter GitHub Actions run logs
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Fetch and filter GitHub Actions run logs.

Downloads run logs via gh CLI and provides filtering by job name,
step name, grep pattern, and failed-only mode.

Pinned selection (issue #101): with no run id, the run is resolved from
the commit at HEAD and the workflow the project declares in its ci.yml,
and an ambiguous choice is refused. Every ``--failed`` message names the
run it read, so "no failed jobs" can never be mistaken for "the build is
fine" when the failing run was a different one.

Anchors and ``--repo`` (issue #97): ``--pr``, ``--branch`` and
``--commit`` reach a run that is not on the current branch head, and
``--repo`` reads a run in another repo. The log archive has no
``--repo`` flag of its own, so the owner and name are substituted into
the API path instead.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from hyperi_ci.common import error, info, warn
from hyperi_ci.gh import RunSelectionError, describe_run, gh_run, require_gh


def logs_api_path(run_id: str, repo: str | None = None) -> str:
    """Build the API path a run's log archive is served from.

    ``{owner}``/``{repo}`` are gh's own placeholders for the cwd's git
    remote; a caller naming another repo needs them substituted, since
    the logs endpoint has no ``--repo`` flag of its own.
    """
    target = repo or "{owner}/{repo}"
    return f"repos/{target}/actions/runs/{run_id}/logs"


def _download_logs(run_id: str, repo: str | None = None) -> Path | None:
    """Download run logs to a temporary directory.

    Args:
        run_id: Workflow run ID.
        repo: Optional ``owner/name``.

    Returns:
        Path to the directory containing log files, or None on error.

    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="hyperi-ci-logs-"))
    zip_path = tmp_dir / "logs.zip"

    # The logs endpoint answers with a redirect to a zip; gh follows it and
    # streams the archive to stdout, which has to land in a file to be opened.
    # (`gh run download` fetches ARTIFACTS, never logs, so it is no fallback.)
    try:
        with zip_path.open("wb") as fh:
            subprocess.run(
                ["gh", "api", logs_api_path(run_id, repo)],
                stdout=fh,
                stderr=subprocess.PIPE,
                check=True,
            )
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_dir)
        zip_path.unlink()
        return tmp_dir
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        error(f"Failed to download logs for run {run_id}: {detail or 'gh api failed'}")
    except zipfile.BadZipFile:
        error(f"Failed to download logs for run {run_id}: the response was not a zip")
    return None


def _get_run(run_id: str, repo: str | None = None) -> dict | None:
    """Fetch a run's identity and its jobs.

    Args:
        run_id: Workflow run ID.
        repo: Optional ``owner/name``.

    Returns:
        Run dict, or None when it could not be read - which is NOT the
        same answer as a run with no failed jobs, and used to be
        reported as one.

    """
    args = [
        "run",
        "view",
        run_id,
        "--json",
        "status,conclusion,jobs,url,workflowName,headBranch,headSha,event",
    ]
    if repo:
        args.extend(["--repo", repo])
    try:
        result = gh_run(args)
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _failed_job_names(run: dict) -> set[str]:
    """Get names of failed jobs in a run, lowercased for matching."""
    return {
        job["name"].lower()
        for job in run.get("jobs", [])
        if job.get("conclusion") == "failure" and job.get("name")
    }


def _run_label(run_id: str, run: dict) -> str:
    """Identify the run a message is talking about."""
    return describe_run({"databaseId": run_id, **run})


def _parse_log_path(path: Path, base: Path) -> tuple[str, str]:
    """Extract job and step names from a log file path.

    Log files follow the pattern: JobName/N_StepName.txt

    Args:
        path: Path to the log file.
        base: Base directory of extracted logs.

    Returns:
        Tuple of (job_name, step_name).

    """
    relative = path.relative_to(base)
    parts = relative.parts

    if len(parts) >= 2:
        job_name = parts[0]
        step_file = parts[-1]
        step_name = re.sub(r"^\d+_", "", step_file.removesuffix(".txt"))
        return job_name, step_name

    return path.stem, ""


def _filter_and_print(
    log_dir: Path,
    *,
    job_filter: str | None = None,
    step_filter: str | None = None,
    grep_pattern: str | None = None,
    tail_lines: int | None = None,
    failed_jobs: set[str] | None = None,
) -> None:
    """Filter and print log files.

    Args:
        log_dir: Directory containing extracted log files.
        job_filter: Substring filter for job names (case-insensitive).
        step_filter: Substring filter for step names (case-insensitive).
        grep_pattern: Regex pattern to match log lines (case-insensitive).
        tail_lines: Only show last N lines per file.
        failed_jobs: Set of failed job names to filter by.

    """
    compiled_grep = re.compile(grep_pattern, re.IGNORECASE) if grep_pattern else None

    log_files = sorted(log_dir.rglob("*.txt"))
    if not log_files:
        warn("No log files found")
        return

    for log_file in log_files:
        job_name, step_name = _parse_log_path(log_file, log_dir)

        if failed_jobs is not None and job_name.lower() not in failed_jobs:
            continue

        if job_filter and job_filter.lower() not in job_name.lower():
            continue

        if step_filter and step_filter.lower() not in step_name.lower():
            continue

        try:
            lines = log_file.read_text(errors="replace").splitlines()
        except OSError:
            continue

        if compiled_grep:
            lines = [line for line in lines if compiled_grep.search(line)]

        if tail_lines is not None:
            lines = lines[-tail_lines:]

        if not lines:
            continue

        prefix = f"[{job_name}]"
        if step_name:
            prefix = f"[{job_name}] [{step_name}]"

        for line in lines:
            print(f"{prefix} {line}")


def fetch_logs(
    *,
    run_id: str | None = None,
    workflow: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    pr: int | None = None,
    repo: str | None = None,
    project_dir: Path | None = None,
    job_filter: str | None = None,
    step_filter: str | None = None,
    grep_pattern: str | None = None,
    tail_lines: int | None = None,
    failed_only: bool = False,
) -> int:
    """Fetch and filter GitHub Actions run logs.

    Args:
        run_id: Run ID. With none, the run is resolved from the anchor,
            and an ambiguous choice is refused rather than guessed
            (issue #101).
        workflow: Workflow name to pin on, matched case-insensitively,
            exact before substring. Defaults to the name declared in the
            project's ci.yml, and only where hyperi-ci scaffolded it.
            Ignored when a run id is given.
        branch: Pin to the newest commit on this branch that has runs.
        commit: Pin to this commit.
        pr: Pin to this pull request's head commit.
        repo: Optional ``owner/name``.
        project_dir: Repo root, for the default pin and the inventory a
            stand-down reads.
        job_filter: Substring filter for job names.
        step_filter: Substring filter for step names.
        grep_pattern: Regex pattern to filter lines.
        tail_lines: Show only last N lines per log file.
        failed_only: Show only failed job logs.

    Returns:
        Exit code: 0=success, 1=error.

    """
    if not require_gh():
        return 1

    if not run_id:
        from hyperi_ci import runs as run_lookup

        try:
            run = run_lookup.resolve(
                workflow=workflow,
                branch=branch,
                commit=commit,
                pr=pr,
                repo=repo,
                project_dir=project_dir,
                command="logs",
            )
        except RunSelectionError as exc:
            error(str(exc))
            return 1
        run_id = str(run["databaseId"])
        info(f"Pinned to run {describe_run(run)}")

    info(f"Fetching logs for run {run_id}")

    failed_jobs: set[str] | None = None
    if failed_only:
        run_data = _get_run(run_id, repo)
        if run_data is None:
            error(
                f"Could not read run {run_id} - cannot tell which jobs failed. "
                f"Check the run id."
            )
            return 1

        failed_jobs = _failed_job_names(run_data)
        if not failed_jobs:
            label = _run_label(run_id, run_data)
            warn(f"No failed jobs in run {label}")
            if run_data.get("status") != "completed":
                warn("  That run has not finished - its jobs may yet go red.")
            elif run_data.get("conclusion") != "failure":
                warn(
                    "  That run did not fail. A failure you are chasing "
                    "belongs to a different run - pass its id, or "
                    "--workflow '<name>' to pin the one you meant."
                )
            return 0

    log_dir = _download_logs(run_id, repo)
    if not log_dir:
        return 1

    _filter_and_print(
        log_dir,
        job_filter=job_filter,
        step_filter=step_filter,
        grep_pattern=grep_pattern,
        tail_lines=tail_lines,
        failed_jobs=failed_jobs,
    )

    return 0
