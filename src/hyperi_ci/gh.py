# Project:   HyperI CI
# File:      src/hyperi_ci/gh.py
# Purpose:   Shared GitHub CLI helpers for trigger, watch, and logs commands
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared GitHub CLI helpers.

Wraps the `gh` CLI, which must be installed and authenticated.

Run selection (issue #101): `watch` and `logs` pin on a commit, narrowed by the
project's declared CI workflow or the one named on the command line, and refuse
an ambiguous choice. "The newest run on the branch" once reported green off a
Dependency Graph run while Test was still going. :func:`select_run` is the
single matcher, and :mod:`hyperi_ci.runs` picks the commit.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from hyperi_ci.common import error, run_cmd
from hyperi_ci.tools import missing_tool_notice

# The default pin for `watch` and `logs` when the caller names no workflow.
_CI_WORKFLOW_FILE = Path(".github/workflows/ci.yml")


class RunSelectionError(Exception):
    """A pinned run lookup did not resolve to exactly one run.

    Carries the message the caller prints before exiting non-zero, always with
    a candidate list or a next command.
    """


# `headSha` is the pin, `workflowName` the narrowing filter, and the rest
# identify the run in a refusal message.
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
        cwd: Repository directory (default: process cwd). A caller honouring
            ``--project-dir`` MUST pass it, or the shell's repo is reported
            (and pushed).

    Returns:
        Branch name, or None if not in a git repo or on a detached HEAD, where
        ``rev-parse --abbrev-ref`` prints the literal ``HEAD``.

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
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command.

    Args:
        args: Arguments to pass to gh (e.g. ["run", "list"]).
        capture: Capture stdout/stderr.
        check: Raise on non-zero exit.
        timeout: Seconds before gh is killed and ``subprocess.TimeoutExpired``
            raised. None waits for it to exit.

    Returns:
        CompletedProcess result.

    """
    return run_cmd(["gh", *args], capture=capture, check=check, timeout=timeout)


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


class ApiResult(NamedTuple):
    """The outcome of one :func:`gh_api` call.

    ``reason`` is a ready-to-print message and is empty on success. ``stderr``
    is gh's own stripped stderr when it exited non-zero, else None, for a
    caller that quotes gh verbatim.
    """

    data: Any
    reason: str
    stderr: str | None


def gh_api(args: list[str], *, body: dict | None = None) -> ApiResult:
    """Call ``gh api`` and parse the JSON response.

    Args:
        args: Arguments after ``gh api`` (endpoint, ``--method`` and so on).
        body: JSON request body, sent through a temp file with ``--input``
            because ``-f key=value`` cannot express an array of objects.

    Returns:
        :class:`ApiResult` with ``data`` None when gh failed or its output was
        not JSON.

    """
    tmp_path: str | None = None
    cmd = ["gh", "api", *args]
    if body is not None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8", newline="\n"
        ) as handle:
            json.dump(body, handle)
            tmp_path = handle.name
        cmd += ["--input", tmp_path]
    try:
        result = run_cmd(cmd, capture=True, check=False)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    endpoint = next((a for a in args if a.startswith("repos/")), args[-1])
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        detail = stderr or f"exit {result.returncode}"
        return ApiResult(None, f"gh api {endpoint} failed: {detail}", stderr)
    try:
        return ApiResult(json.loads(result.stdout), "", None)
    except ValueError as exc:
        return ApiResult(None, f"gh api {endpoint} returned no JSON: {exc}", None)


def gh_json_or_none(args: list[str]) -> object | None:
    """Run a gh command and decode its JSON, or None on any failure."""
    result = gh_run(args, check=False)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def repo_file(full_name: str, path: str) -> str | None:
    """Fetch one file's raw text from a repo's DEFAULT branch.

    None when the file is missing or unreadable. A local clone parked on a fix
    branch would report a fix that main lacks, so audits read it this way.
    """
    result = gh_run(
        [
            "api",
            f"repos/{full_name}/contents/{path}",
            "--header",
            "Accept: application/vnd.github.raw+json",
        ],
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def org_repos(org: str) -> list[str]:
    """Return every non-archived repo in the org as ``owner/name``."""
    data = gh_json_or_none(
        ["api", f"orgs/{org}/repos?per_page=100&type=all", "--paginate"]
    )
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("archived"):
            continue
        full_name = entry.get("full_name")
        if isinstance(full_name, str):
            names.append(full_name)
    return names


def get_latest_run(
    branch: str | None = None,
    workflow: str | None = None,
    repo: str | None = None,
) -> dict | None:
    """Find the most recent workflow run.

    Args:
        branch: Filter by branch name.
        workflow: Filter by workflow filename.
        repo: Optional ``owner/name``, queried instead of the cwd's remote.

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

    The workflow filter is NOT passed to `gh`: `gh run list --workflow` takes a
    name or a filename, and mixing that with :func:`select_run`'s name matching
    would silently drop runs.

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
        RunSelectionError: nothing matched, or several runs did. Picking the
            newest of several is the issue #101 bug.

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
