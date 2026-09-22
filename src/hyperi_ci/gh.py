# Project:   HyperI CI
# File:      src/hyperi_ci/gh.py
# Purpose:   Shared GitHub CLI helpers for trigger, watch, and logs commands
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared GitHub CLI helpers.

Provides common utilities for interacting with GitHub Actions via the `gh` CLI.
All commands require `gh` to be installed and authenticated.

Run selection (issue #101): `watch` and `logs` pin on the run they were
asked about - a commit, narrowed by the project's declared CI workflow
or the one named on the command line - and refuse when the choice is
ambiguous. Falling back to "the newest run on the branch" is how a watch
reported green off a Dependency Graph run while the Test run was still
going. :func:`select_run` is the single matcher; which commit to pin on
is :mod:`hyperi_ci.runs`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

from hyperi_ci.common import error, run_cmd
from hyperi_ci.tools import missing_tool_notice

# The workflow a project declares as its own CI, and the default pin for
# `watch` and `logs` when the caller names none.
_CI_WORKFLOW_FILE = Path(".github/workflows/ci.yml")


class RunSelectionError(Exception):
    """A pinned run lookup did not resolve to exactly one run.

    Carries the message the caller prints before exiting non-zero. Every
    path that raises this has a candidate list or a next command in the
    message - a refusal the user cannot act on is no better than a guess.
    """


# Fields every run-selection decision reads. `headSha` is the pin,
# `workflowName` the narrowing filter, and the rest identify the run in
# the message a refusal prints.
RUN_LIST_FIELDS = [
    "databaseId",
    "status",
    "conclusion",
    "headBranch",
    "headSha",
    "event",
    "workflowName",
    "createdAt",
    "updatedAt",
    "url",
]


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


def get_head_sha(*, cwd: str | None = None) -> str | None:
    """Get the full commit sha at HEAD.

    Args:
        cwd: Repository directory (default: process cwd).

    Returns:
        The 40-character sha, or None outside a git repo.

    """
    try:
        result = run_cmd(
            ["git", "rev-parse", "HEAD"],
            capture=True,
            check=True,
            cwd=cwd,
        )
    except subprocess.CalledProcessError:
        return None
    return result.stdout.strip() or None


def list_runs(
    *,
    branch: str | None = None,
    commit: str | None = None,
    repo: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """List workflow runs, newest first.

    The workflow filter is deliberately NOT passed to `gh`: `gh run list
    --workflow` accepts a name or a filename, and mixing that with the
    name matching in :func:`select_run` would silently drop runs. One
    matcher owns the decision.

    Args:
        branch: Filter by branch name.
        commit: Filter by the sha the run was built from.
        repo: Optional ``owner/name`` - defaults to the cwd's git remote.
        limit: Maximum runs to fetch.

    Returns:
        List of run dicts carrying :data:`RUN_LIST_FIELDS`.

    """
    args = ["run", "list", "--limit", str(limit)]
    if repo:
        args.extend(["--repo", repo])
    if branch:
        args.extend(["--branch", branch])
    if commit:
        args.extend(["--commit", commit])

    return gh_json(args, RUN_LIST_FIELDS)


def describe_run(run: dict) -> str:
    """Format one run for a refusal message or a "watching X" line."""
    return (
        f"{run.get('databaseId', '?')}  {run.get('workflowName', '?')}"
        f"  [{run.get('event', '?')}]"
        f"  {run.get('status', '?')}/{run.get('conclusion') or 'pending'}"
        f"  {run.get('url', '')}"
    ).rstrip()


def _workflow_candidates(runs: list[dict], workflow: str) -> list[dict]:
    """Narrow runs to a workflow name, exact match before substring."""
    wanted = workflow.strip().lower()
    names = [(run, (run.get("workflowName") or "").strip().lower()) for run in runs]
    exact = [run for run, name in names if name == wanted]
    return exact or [run for run, name in names if wanted and wanted in name]


def select_run(
    runs: list[dict],
    *,
    head_sha: str | None = None,
    workflow: str | None = None,
) -> dict:
    """Pick the one run the caller asked about.

    Args:
        runs: Candidate runs, as returned by :func:`list_runs`.
        head_sha: When set, only runs built from this commit qualify.
        workflow: When set, only runs whose workflow name matches
            (case-insensitive, exact before substring) qualify.

    Returns:
        The single matching run.

    Raises:
        RunSelectionError: nothing matched, or several runs did. Picking
            the newest of several is the issue #101 bug - a conclusion
            reported for a run nobody asked about.

    """
    candidates = list(runs)

    if head_sha:
        pin = head_sha.lower()
        candidates = [
            run for run in candidates if (run.get("headSha") or "").lower() == pin
        ]

    if not candidates:
        pinned = f" for commit {head_sha[:8]}" if head_sha else ""
        raise RunSelectionError(f"No runs found{pinned}")

    if workflow:
        matched = _workflow_candidates(candidates, workflow)
        if not matched:
            names = ", ".join(
                sorted({run.get("workflowName") or "?" for run in candidates})
            )
            raise RunSelectionError(
                f"No run matches workflow '{workflow}'. Workflows on this "
                f"commit: {names}"
            )
        candidates = matched

    if len(candidates) > 1:
        listing = "\n".join(f"  {describe_run(run)}" for run in candidates)
        narrow = (
            "Narrow it with --workflow '<name>', or pass one of the run ids above."
            if not workflow
            else "Pass one of the run ids above."
        )
        raise RunSelectionError(
            f"{len(candidates)} runs match - refusing to guess which one you "
            f"meant.\n{listing}\n{narrow}"
        )

    return candidates[0]


def project_ci_workflow(*, cwd: Path | None = None) -> str | None:
    """Read the workflow name this project declares in its ci.yml.

    Args:
        cwd: Project root (default: process cwd).

    Returns:
        The declared workflow name, or None when the file is absent or
        names nothing.

    """
    path = (cwd or Path.cwd()) / _CI_WORKFLOW_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def get_run_jobs(run_id: str) -> list[dict]:
    """Get jobs for a specific run.

    Args:
        run_id: The workflow run ID.

    Returns:
        List of job dicts with name, status, conclusion, steps.

    """
    return gh_json(["run", "view", run_id, "--json", "jobs"], ["jobs"])
