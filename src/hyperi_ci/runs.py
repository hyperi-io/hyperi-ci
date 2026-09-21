# Project:   HyperI CI
# File:      src/hyperi_ci/runs.py
# Purpose:   Resolve which run the caller meant, and stand down when it cannot
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve the run `watch`, `logs` and `rerun` were asked about.

Issue #101 pinned run selection to the commit at HEAD, which fixed a
watch reporting green off a Dependency Graph run but left every run that
is not on HEAD unreachable: a `pull_request` run after a local amend, a
`schedule` run on main while you are on a branch, a run on a PR branch
you have not checked out. `watch` and `logs` answered "No runs found"
while the run sat there (issue #97).

Four anchors now say which commit to pin on -- HEAD by default, or
``--commit``, ``--branch``, ``--pr`` -- and the #101 discipline is
unchanged behind each: the workflow narrows the candidates, and an
ambiguous choice is refused rather than guessed.

Where nothing resolves, the refusal STANDS DOWN: it lists the runs
GitHub does hold, marks the workflows hyperi-ci did not scaffold, and
prints the command that reaches the run. A refusal the caller cannot act
on is what sent them to the native CLI in the first place.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci import workflows
from hyperi_ci.gh import (
    RunSelectionError,
    describe_run,
    get_head_sha,
    gh_run,
    list_runs,
    project_ci_workflow,
    select_run,
)

# Headroom over the handful of runs one commit produces, and enough of a
# listing to be useful when the wrapper stands down.
RUN_LIST_LIMIT = 30

# Runs shown in a stand-down. Long enough to carry every workflow a repo
# fires on one commit, short enough to read.
_STAND_DOWN_LIMIT = 15


@dataclass(frozen=True, slots=True)
class Anchor:
    """What the caller pinned the lookup to.

    Attributes:
        sha: Commit the run must have been built from, or None when the
            lookup is repo-wide and nothing narrows it.
        branch: Branch the anchor came from, where one is known.
        label: How the anchor reads in a message, e.g. ``PR #18``.
        repo: Target ``owner/name``, or None for the cwd's git remote.
        local: The target is this checkout's own repo, so the workflow it
            declares in ci.yml is a legitimate default pin. True for a
            PR or branch in this repo, not only for HEAD.
        head: The anchor is the commit at HEAD, so a run may still be
            registering and is worth waiting for.

    """

    sha: str | None
    branch: str | None
    label: str
    repo: str | None
    local: bool
    head: bool = False


def _one_anchor(branch: str | None, commit: str | None, pr: int | None) -> None:
    """Refuse two anchors at once rather than silently honouring one."""
    given = [
        name
        for name, value in (("--branch", branch), ("--commit", commit), ("--pr", pr))
        if value
    ]
    if len(given) > 1:
        raise RunSelectionError(
            f"{' and '.join(given)} both pin the lookup - pass one of them."
        )


def pr_head(number: int, *, repo: str | None = None) -> tuple[str, str]:
    """Read a pull request's head commit and branch.

    A ``pull_request`` run records the PR's head commit as its headSha,
    so the PR number resolves to the same pin a local checkout would.

    Args:
        number: Pull request number.
        repo: Optional ``owner/name``.

    Returns:
        Tuple of (head sha, head branch name).

    Raises:
        RunSelectionError: The PR could not be read.

    """
    args = ["pr", "view", str(number), "--json", "headRefOid,headRefName"]
    if repo:
        args.extend(["--repo", repo])
    try:
        result = gh_run(args)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RunSelectionError(
            f"Could not read PR #{number}"
            + (f" in {repo}" if repo else "")
            + " - check the number, or pass a run id."
        ) from exc
    sha = (data.get("headRefOid") or "").strip()
    if not sha:
        raise RunSelectionError(f"PR #{number} reports no head commit")
    return sha, (data.get("headRefName") or "").strip()


def _branch_head_sha(branch: str, *, repo: str | None) -> str | None:
    """Return the newest commit on a branch that GitHub holds a run for."""
    runs = list_runs(branch=branch, repo=repo, limit=RUN_LIST_LIMIT)
    for run in runs:
        sha = (run.get("headSha") or "").strip()
        if sha:
            return sha
    return None


def resolve_anchor(
    *,
    branch: str | None = None,
    commit: str | None = None,
    pr: int | None = None,
    repo: str | None = None,
) -> Anchor:
    """Work out which commit the caller meant.

    Args:
        branch: Pin to the newest commit on this branch that has runs.
        commit: Pin to this commit.
        pr: Pin to this pull request's head commit.
        repo: Optional ``owner/name``.

    Returns:
        The Anchor. With no selector and no ``repo``, HEAD is the pin;
        with ``repo`` and no selector the lookup is repo-wide and carries
        no sha, because a local HEAD says nothing about another repo.

    Raises:
        RunSelectionError: Two anchors were given, or the pin could not
            be read.

    """
    _one_anchor(branch, commit, pr)

    # Another repo's ci.yml is not this checkout's to assume, so only a
    # lookup against this repo carries a default pin.
    local = repo is None

    if pr:
        sha, head_branch = pr_head(pr, repo=repo)
        return Anchor(sha, head_branch or None, f"PR #{pr}", repo, local=local)

    if commit:
        return Anchor(commit, None, f"commit {commit[:8]}", repo, local=local)

    if branch:
        sha = _branch_head_sha(branch, repo=repo)
        return Anchor(sha, branch, f"branch {branch}", repo, local=local)

    if repo:
        return Anchor(None, None, repo, repo, local=False)

    head_sha = get_head_sha()
    if not head_sha:
        raise RunSelectionError(
            "Could not read HEAD - pass a run id, or pin the lookup with "
            "--branch / --commit / --pr."
        )
    return Anchor(
        head_sha, None, f"commit {head_sha[:8]} (HEAD)", None, local=True, head=True
    )


def anchor_runs(anchor: Anchor, *, limit: int = RUN_LIST_LIMIT) -> list[dict]:
    """List the runs GitHub holds against an anchor.

    Args:
        anchor: The resolved anchor.
        limit: Maximum runs to fetch.

    Returns:
        Run dicts, newest first.

    Raises:
        RunSelectionError: The listing could not be fetched.

    """
    try:
        return list_runs(
            commit=anchor.sha,
            branch=None if anchor.sha else anchor.branch,
            repo=anchor.repo,
            limit=limit,
        )
    except subprocess.CalledProcessError as exc:
        raise RunSelectionError(
            f"Could not list runs for {anchor.label} - pass a run id."
        ) from exc


def _default_pin(anchor: Anchor, project_dir: Path | None) -> str | None:
    """Return the project's own CI workflow name, where it is a fair default.

    The name comes from the repo's own ci.yml, so it is the right default
    whoever wrote that file -- gating it on whether hyperi-ci scaffolded
    the workflow would leave every fork refusing among CodeQL and the
    scheduled audits, which is the failing closed light touch forbids.
    Only the repo the checkout sits in can be read this way.
    """
    if not anchor.local:
        return None
    return project_ci_workflow(cwd=project_dir)


def stand_down(
    anchor: Anchor,
    *,
    reason: str,
    command: str,
    project_dir: Path | None = None,
) -> str:
    """Build a refusal the caller can act on.

    Lists the runs GitHub does hold, marks the workflows hyperi-ci did
    not scaffold, and prints the command that reaches one. Reporting only
    "No runs found" is what sent callers to the native CLI.

    Args:
        anchor: The anchor that resolved nothing.
        reason: What went wrong, as the first line.
        command: The hyperi-ci command being run, e.g. ``watch``.
        project_dir: Repo root, for reading which workflows are ours.

    Returns:
        The message to print before exiting non-zero.

    """
    lines = [reason]

    scope = Anchor(None, anchor.branch, anchor.label, anchor.repo, anchor.local)
    # Listing the whole repo when the branch has nothing is the point of
    # standing down, so a branch with no runs widens rather than repeats.
    try:
        seen = anchor_runs(scope, limit=_STAND_DOWN_LIMIT)
    except RunSelectionError:
        seen = []

    repo_label = f" in {anchor.repo}" if anchor.repo else ""
    if not seen:
        lines.append(f"GitHub holds no recent runs{repo_label} either.")
        return "\n".join(lines)

    where = f" on branch {anchor.branch}" if anchor.branch else repo_label
    lines.append(f"Recent runs{where}:")
    lines.extend(f"  {describe_run(run)}" for run in seen)

    # A foreign repo's workflow files are not in this checkout, so its
    # ownership cannot be read from the local inventory.
    if anchor.repo is None:
        inventory = workflows.inventory(project_dir)
        ours = {name.lower() for name in workflows.owned_names(inventory)}
        outside = sorted(
            {
                run.get("workflowName") or "?"
                for run in seen
                if (run.get("workflowName") or "").lower() not in ours
            }
        )
        if inventory and outside:
            lines.append(
                f"hyperi-ci did not scaffold {', '.join(outside)} - it wraps "
                f"them, it does not own them."
            )

    repo_arg = f" --repo {anchor.repo}" if anchor.repo else ""
    first = seen[0].get("databaseId", "<run-id>")
    lines.append(f"Reach one directly: hyperi-ci {command} {first}{repo_arg}")
    return "\n".join(lines)


def resolve(
    *,
    workflow: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    pr: int | None = None,
    repo: str | None = None,
    project_dir: Path | None = None,
    command: str = "watch",
    limit: int = RUN_LIST_LIMIT,
) -> dict:
    """Resolve the one run the caller meant.

    Args:
        workflow: Workflow name to narrow on. With none, the project's
            own scaffolded CI workflow is the default pin -- and only
            when the lookup is anchored in this checkout.
        branch: Pin to the newest commit on this branch that has runs.
        commit: Pin to this commit.
        pr: Pin to this pull request's head commit.
        repo: Optional ``owner/name``.
        project_dir: Repo root, for the default pin and the workflow
            inventory a stand-down reads.
        command: The hyperi-ci command, named in the stand-down.
        limit: Maximum runs to fetch.

    Returns:
        The single matching run.

    Raises:
        RunSelectionError: Nothing matched, or the choice is ambiguous.
            The message stands down with the runs that do exist.

    """
    anchor = resolve_anchor(branch=branch, commit=commit, pr=pr, repo=repo)
    require_sha(anchor, command=command, project_dir=project_dir)
    return pick(
        anchor,
        anchor_runs(anchor, limit=limit),
        workflow=workflow,
        project_dir=project_dir,
        command=command,
    )


def require_sha(
    anchor: Anchor,
    *,
    command: str,
    project_dir: Path | None = None,
) -> None:
    """Refuse an anchor that pins no commit, listing what is there instead.

    Raises:
        RunSelectionError: The anchor carries no sha.

    """
    if anchor.sha is not None:
        return
    raise RunSelectionError(
        stand_down(
            anchor,
            reason=(
                f"Nothing pins the lookup for {anchor.label} - a run id, "
                f"--branch, --commit or --pr says which run you mean."
            ),
            command=command,
            project_dir=project_dir,
        )
    )


def pick(
    anchor: Anchor,
    runs: list[dict],
    *,
    workflow: str | None = None,
    project_dir: Path | None = None,
    command: str = "watch",
) -> dict:
    """Choose the one run at an anchor, refusing an ambiguous choice.

    Args:
        anchor: The resolved anchor; its sha is the pin.
        runs: Candidate runs, as :func:`anchor_runs` returns them.
        workflow: Workflow name to narrow on, else the project's default
            pin where the anchor is local and hyperi-ci owns a workflow.
        project_dir: Repo root, for the default pin and the inventory.
        command: The hyperi-ci command, named in the stand-down.

    Returns:
        The single matching run.

    Raises:
        RunSelectionError: Nothing matched, or several did.

    """
    pin = workflow or _default_pin(anchor, project_dir)

    try:
        return select_run(runs, head_sha=anchor.sha, workflow=pin)
    except RunSelectionError as narrow:
        if workflow or pin is None:
            raise RunSelectionError(
                _widen(narrow, anchor, command=command, project_dir=project_dir)
            ) from narrow

    # The default pin resolved nothing, so refuse against every candidate
    # rather than only the ones its own guess allowed through.
    try:
        return select_run(runs, head_sha=anchor.sha)
    except RunSelectionError as exc:
        raise RunSelectionError(
            _widen(exc, anchor, command=command, project_dir=project_dir)
        ) from exc


def _widen(
    exc: RunSelectionError,
    anchor: Anchor,
    *,
    command: str,
    project_dir: Path | None,
) -> str:
    """Attach a stand-down to a refusal that found nothing at the anchor.

    An ambiguity already names every candidate, so it is left as it is;
    only an empty result needs the wider listing.
    """
    message = str(exc)
    if "No runs found" not in message:
        return message
    return stand_down(
        anchor,
        reason=f"No runs found for {anchor.label}.",
        command=command,
        project_dir=project_dir,
    )
