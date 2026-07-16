# Project:   HyperI CI
# File:      src/hyperi_ci/gh.py
# Purpose:   Shared GitHub CLI helpers for trigger, watch, and logs commands
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
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
from hyperi_ci.tools import missing_tool_notice


def require_gh() -> bool:
    """Check that the gh CLI is installed and accessible.

    Returns:
        True if gh is available, False otherwise.

    """
    if not shutil.which("gh"):
        error(missing_tool_notice("gh"))
        return False
    return True


def get_current_branch(*, cwd: str | None = None) -> str | None:
    """Get the current git branch name.

    Args:
        cwd: Repository directory (default: process cwd). Callers that
            honour a ``--project-dir`` MUST pass it — otherwise the
            branch of whatever repo the shell happens to sit in is
            reported (and pushed).

    Returns:
        Branch name, or None if not in a git repo or on a detached HEAD
        (``rev-parse --abbrev-ref`` reports the literal ``HEAD`` there —
        not a pushable branch name).

    """
    try:
        result = run_cmd(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture=True,
            check=True,
            cwd=cwd,
        )
    except subprocess.CalledProcessError:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


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
    repo: str | None = None,
) -> dict | None:
    """Find the most recent workflow run.

    Args:
        branch: Filter by branch name.
        workflow: Filter by workflow filename.
        repo: Optional ``owner/name`` — when set, queries this repo
            instead of the cwd's git remote. Use this when looking up
            runs in a different repo than your cwd.

    Returns:
        Dict with run info, or None if no runs found.

    """
    args = ["run", "list", "--limit", "1"]
    if repo:
        args.extend(["--repo", repo])
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
