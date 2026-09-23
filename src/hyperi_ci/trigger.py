# Project:   HyperI CI
# File:      src/hyperi_ci/trigger.py
# Purpose:   Trigger a GitHub Actions workflow run
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Trigger GitHub Actions workflow runs.

Dispatches a workflow_dispatch event via the gh CLI, waits for the run
to appear, and optionally watches it to completion.

Light touch (issue #97): the workflow is whatever the caller names, not
only the ci.yml hyperi-ci scaffolds. A display name or a bare stem
resolves to the file -- ``-w upstream-sync`` reaches
``upstream-sync.yml`` -- and a workflow hyperi-ci did not scaffold is
dispatched all the same. A token this checkout cannot see is passed to
gh unchanged: refusing off a local listing would fail closed on a repo
whose conventions hyperi-ci does not set, and the checkout may simply
lag the remote. The inventory becomes a hint only after gh says no.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path

from hyperi_ci import workflows as workflow_files
from hyperi_ci.common import error, info, success, warn
from hyperi_ci.gh import get_current_branch, get_latest_run, gh_run, require_gh


def resolve_workflow_file(token: str, project_dir: Path | None = None) -> str:
    """Map what the caller typed to the workflow filename gh dispatches.

    Args:
        token: Filename, filename stem, or the workflow's display name.
        project_dir: Repo root (default: process cwd).

    Returns:
        The filename to pass to ``gh workflow run``. An inventory that
        cannot be read, or a token naming nothing in it, is returned
        unchanged so a caller working against another repo is not
        blocked by this checkout's contents.

    """
    inventory = workflow_files.inventory(project_dir)
    if not inventory:
        return token
    match = workflow_files.find(inventory, token)
    return match.filename if match else token


def _wait_for_run(
    branch: str,
    workflow: str,
    before_time: float,
    max_wait: int = 60,
    repo: str | None = None,
) -> str | None:
    """Wait for a new run to appear after triggering.

    Polls every 2 seconds for up to max_wait seconds, looking for a run
    that was created after before_time.

    Args:
        branch: Branch the run should be on.
        workflow: Workflow filename.
        before_time: Unix timestamp before the trigger was sent.
        max_wait: Maximum seconds to wait.
        repo: Optional ``owner/name``.

    Returns:
        Run ID as string, or None if no run appeared.

    """
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        time.sleep(2)
        run = get_latest_run(branch=branch, workflow=workflow, repo=repo)
        if not (run and run.get("databaseId")):
            continue
        # Filter out the previous run still showing as "latest" -- gh's
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


def parse_inputs(pairs: list[str] | None) -> dict[str, str]:
    """Parse ``key=value`` dispatch inputs, keeping the order given.

    A value may itself contain ``=``; only the first one separates.

    Raises:
        ValueError: An entry carries no ``=`` or an empty key, which gh would
            otherwise take as a positional argument and ignore.

    """
    parsed: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--input must be key=value, got: {pair!r}")
        parsed[key.strip()] = value
    return parsed


def trigger_workflow(
    *,
    workflow: str = "ci.yml",
    ref: str | None = None,
    inputs: dict[str, str] | None = None,
    watch: bool = False,
    timeout: int = 1800,
    interval: int = 30,
    repo: str | None = None,
    project_dir: Path | None = None,
) -> int:
    """Trigger a GitHub Actions workflow run.

    Args:
        workflow: Workflow filename, stem or display name (e.g. "ci.yml",
            "upstream-sync"). Any workflow in the repo qualifies, not
            only the one hyperi-ci scaffolded.
        ref: Branch or tag to run on. Defaults to current branch.
        inputs: workflow_dispatch inputs, each sent as ``-f key=value``.
            A workflow declaring required inputs cannot be dispatched
            without them (issue #97).
        watch: If True, watch the run to completion after triggering.
        timeout: Watch timeout in seconds.
        interval: Watch poll interval in seconds.
        repo: Optional ``owner/name`` -- dispatch into another repo
            instead of the cwd's git remote.
        project_dir: Repo root, for resolving the workflow name.

    Returns:
        Exit code: 0=success, 1=failed, 2=timeout.

    """
    if not require_gh():
        return 1

    branch = ref or get_current_branch()
    if not branch:
        error("Could not detect current branch — use --ref to specify")
        return 1

    target = workflow if repo else resolve_workflow_file(workflow, project_dir)

    cmd = ["workflow", "run", target, "--ref", branch]
    if repo:
        cmd.extend(["--repo", repo])
    for key, value in (inputs or {}).items():
        cmd.extend(["-f", f"{key}={value}"])

    where = f"{branch} in {repo}" if repo else branch
    if inputs:
        rendered = ", ".join(f"{k}={v}" for k, v in inputs.items())
        info(f"Triggering {target} on {where} with {rendered}")
    else:
        info(f"Triggering {target} on {where}")

    try:
        gh_run(cmd, capture=False, check=True)
    except subprocess.CalledProcessError:
        error(f"Failed to trigger workflow {target}{_carried(repo, project_dir)}")
        return 1

    success(f"Triggered {target} on {where}")

    if not watch:
        return 0

    info("Waiting for run to appear...")
    before = time.time()
    run_id = _wait_for_run(branch, target, before, repo=repo)
    if not run_id:
        warn("Run did not appear within 60 seconds")
        return 2

    info(f"Run {run_id} started — watching...")

    from hyperi_ci.watch import watch_run

    return watch_run(run_id=run_id, timeout=timeout, interval=interval, repo=repo)


def _carried(repo: str | None, project_dir: Path | None) -> str:
    """Name the workflows this checkout carries, as a hint after a failure.

    Only a hint: the checkout may lag the remote, and the workflow gh
    could not find may exist on the default branch regardless. Refusing
    up front off a local listing would fail closed on a repo whose
    conventions hyperi-ci does not set.
    """
    if repo:
        return ""
    inventory = workflow_files.inventory(project_dir)
    if not inventory:
        return ""
    available = ", ".join(sorted(wf.filename for wf in inventory))
    return f". This checkout carries: {available}"
