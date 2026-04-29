# Project:   HyperI CI
# File:      src/hyperi_ci/logs.py
# Purpose:   Fetch and filter GitHub Actions run logs
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Fetch and filter GitHub Actions run logs.

Downloads run logs via gh CLI and provides filtering by job name,
step name, grep pattern, and failed-only mode.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from hyperi_ci.common import error, info, warn
from hyperi_ci.gh import get_current_branch, get_latest_run, gh_run, require_gh


def _download_logs(run_id: str) -> Path | None:
    """Download run logs to a temporary directory.

    Args:
        run_id: Workflow run ID.

    Returns:
        Path to the directory containing log files, or None on error.

    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="hyperi-ci-logs-"))

    try:
        gh_run(
            ["run", "download", run_id, "--dir", str(tmp_dir)],
            capture=True,
            check=True,
        )
        return tmp_dir
    except subprocess.CalledProcessError:
        pass

    try:
        zip_path = tmp_dir / "logs.zip"
        gh_run(
            ["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/logs"],
            capture=True,
            check=True,
        )
        if zip_path.exists():
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_dir)
            zip_path.unlink()
            return tmp_dir
    except (subprocess.CalledProcessError, zipfile.BadZipFile):
        pass

    error(f"Failed to download logs for run {run_id}")
    return None


def _get_failed_jobs(run_id: str) -> set[str]:
    """Get names of failed jobs in a run.

    Args:
        run_id: Workflow run ID.

    Returns:
        Set of failed job names (lowercased for matching).

    """
    try:
        result = gh_run(
            [
                "run",
                "view",
                run_id,
                "--json",
                "jobs",
            ]
        )
        data = json.loads(result.stdout)
        jobs = data.get("jobs", [])
        return {
            job["name"].lower() for job in jobs if job.get("conclusion") == "failure"
        }
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        return set()


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
    job_filter: str | None = None,
    step_filter: str | None = None,
    grep_pattern: str | None = None,
    tail_lines: int | None = None,
    failed_only: bool = False,
) -> int:
    """Fetch and filter GitHub Actions run logs.

    Args:
        run_id: Run ID. Auto-detects latest on current branch if None.
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

    info(f"Fetching logs for run {run_id}")

    failed_jobs: set[str] | None = None
    if failed_only:
        failed_jobs = _get_failed_jobs(run_id)
        if not failed_jobs:
            warn("No failed jobs found")
            return 0

    log_dir = _download_logs(run_id)
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
