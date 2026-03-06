# Project:   HyperI CI
# File:      src/hyperi_ci/gh.py
# Purpose:   Shared GitHub CLI helpers for trigger, watch, and logs commands
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared GitHub CLI helpers.

Provides common utilities for interacting with GitHub Actions via the `gh` CLI.
All commands require `gh` to be installed and authenticated.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from hyperi_ci.common import error, run_cmd


def require_gh() -> bool:
    """Check that the gh CLI is installed and accessible.

    Returns:
        True if gh is available, False otherwise.
    """
    if not shutil.which("gh"):
        error("gh CLI not found — install from https://cli.github.com/")
        return False
    return True


def get_current_branch() -> str | None:
    """Get the current git branch name.

    Returns:
        Branch name, or None if not in a git repo.
    """
    try:
        result = run_cmd(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def gh_run(
    args: list[str],
    *,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command.

    Args:
        args: Arguments to pass to gh (e.g. ["run", "list"]).
        capture: Capture stdout/stderr.
        check: Raise on non-zero exit.

    Returns:
        CompletedProcess result.
    """
    return run_cmd(["gh", *args], capture=capture, check=check)


def gh_json(
    args: list[str],
    fields: list[str],
) -> list[dict]:
    """Run a gh CLI command with --json output and parse the result.

    Args:
        args: Base gh arguments (e.g. ["run", "list"]).
        fields: JSON field names to request.

    Returns:
        List of dicts with the requested fields.
    """
    result = gh_run([*args, "--json", ",".join(fields)])
    return json.loads(result.stdout)


def get_latest_run(
    branch: str | None = None,
    workflow: str | None = None,
) -> dict | None:
    """Find the most recent workflow run.

    Args:
        branch: Filter by branch name.
        workflow: Filter by workflow filename.

    Returns:
        Dict with run info, or None if no runs found.
    """
    args = ["run", "list", "--limit", "1"]
    if branch:
        args.extend(["--branch", branch])
    if workflow:
        args.extend(["--workflow", workflow])

    fields = [
        "databaseId",
        "status",
        "conclusion",
        "headBranch",
        "event",
        "workflowName",
        "createdAt",
        "updatedAt",
        "url",
    ]

    runs = gh_json(args, fields)
    if not runs:
        return None
    return runs[0]


def get_run_jobs(run_id: str) -> list[dict]:
    """Get jobs for a specific run.

    Args:
        run_id: The workflow run ID.

    Returns:
        List of job dicts with name, status, conclusion, steps.
    """
    return gh_json(["run", "view", run_id, "--json", "jobs"], ["jobs"])
