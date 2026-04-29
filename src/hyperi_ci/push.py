# Project:   HyperI CI
# File:      src/hyperi_ci/push.py
# Purpose:   Push wrapper with pre-checks and meta-operations
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Push wrapper with pre-checks and meta-operations.

Wraps git push with:
- Pre-push validation (hyperi-ci check)
- Auto-rebase to sync semantic-release commits
- --release: auto-dispatch publish after CI passes
- --no-ci: amend last commit with [skip ci] marker

All flows set HYPERCI_PUSH=1 so the pre-push hook allows the push.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.gh import get_current_branch, get_latest_run, require_gh


def push(
    *,
    release: bool = False,
    no_ci: bool = False,
    dry_run: bool = False,
    force: bool = False,
    project_dir: Path | None = None,
) -> int:
    """Push with pre-checks and optional meta-operations.

    Args:
        release: After CI passes, auto-dispatch publish for new version.
        no_ci: Amend last commit with [skip ci] and push.
        dry_run: Show what would happen without executing.
        force: Skip hyperi-ci check step.
        project_dir: Project directory (default: cwd).

    Returns:
        Exit code: 0=success, non-zero=failure.

    """
    if release and no_ci:
        error("--release and --no-ci are mutually exclusive")
        return 1

    cwd = str(project_dir) if project_dir else None

    if no_ci:
        return _skip_ci_push(dry_run=dry_run, cwd=cwd)

    if release:
        return _release_push(dry_run=dry_run, force=force, cwd=cwd)

    return _default_push(dry_run=dry_run, force=force, cwd=cwd)


def _default_push(*, dry_run: bool, force: bool, cwd: str | None) -> int:
    """Check, rebase, push."""
    if rc := _check_dirty_tree(cwd=cwd):
        return rc

    if not force:
        if rc := _run_check(cwd=cwd):
            return rc

    if dry_run:
        info("Dry run: would rebase and push")
        return 0

    return _rebase_and_push(cwd=cwd)


def _release_push(*, dry_run: bool, force: bool, cwd: str | None) -> int:
    """Check, rebase, push, watch CI, detect tag, dispatch publish, watch."""
    if not require_gh():
        return 1

    branch = get_current_branch()
    if branch != "main":
        error("--release only works from main")
        return 1

    if rc := _check_dirty_tree(cwd=cwd):
        return rc

    if not force:
        if rc := _run_check(cwd=cwd):
            return rc

    if dry_run:
        info("Dry run: would rebase, push, watch CI, and dispatch publish")
        return 0

    before_tags = _get_current_tags(cwd=cwd)
    before_run_id = _get_latest_run_id("main")

    rc = _rebase_and_push(branch="main", cwd=cwd)
    if rc != 0:
        return rc

    run_id = _poll_for_new_run("main", before_run_id)
    if not run_id:
        error("CI run did not appear within 30 seconds")
        info("Check manually: hyperi-ci watch")
        return 1

    info(f"CI run {run_id} started — watching...")

    from hyperi_ci.watch import watch_run

    rc = watch_run(run_id=run_id)
    if rc != 0:
        error("CI failed — publish not attempted")
        return rc

    new_tag = _detect_new_tag(before_tags, cwd=cwd)
    if not new_tag:
        info("No version bump from these commits — nothing to publish")
        return 0

    info(f"New version tag: {new_tag}")

    from hyperi_ci.release import dispatch_publish

    rc = dispatch_publish(new_tag)
    if rc != 0:
        warn(f"Publish dispatch failed — run manually: hyperi-ci release {new_tag}")
        return rc

    info("Watching publish run...")
    return watch_run()


def _skip_ci_push(*, dry_run: bool, cwd: str | None) -> int:
    """Amend last commit with [skip ci], push with --force-with-lease."""
    if rc := _check_dirty_tree(cwd=cwd):
        return rc

    if rc := _check_not_ci_commit(cwd=cwd):
        return rc

    msg = _get_last_commit_message(cwd=cwd)
    if not msg:
        error("Could not read last commit message")
        return 1

    if "[skip ci]" in msg:
        warn("Last commit already contains [skip ci]")
        if dry_run:
            return 0
        return _push_with_env(args=["--force-with-lease"], cwd=cwd)

    new_msg = f"{msg} [skip ci]"

    if dry_run:
        info(f"Dry run: would amend commit message to: {new_msg}")
        return 0

    try:
        run_cmd(
            ["git", "commit", "--amend", "-m", new_msg],
            cwd=cwd,
            capture=True,
        )
    except subprocess.CalledProcessError:
        error("Failed to amend commit")
        return 1

    info("Amended commit with [skip ci]")
    return _push_with_env(args=["--force-with-lease"], cwd=cwd)


# --- helpers ---


def _check_dirty_tree(*, cwd: str | None) -> int:
    """Check for uncommitted changes. Returns 0 if clean, 1 if dirty."""
    result = run_cmd(
        ["git", "status", "--porcelain"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if result.stdout.strip():
        error("Uncommitted changes. Commit or stash first.")
        return 1
    return 0


def _check_not_ci_commit(*, cwd: str | None) -> int:
    """Check last commit is not a semantic-release version commit. Returns 0 if OK."""
    msg = _get_last_commit_message(cwd=cwd)
    if not msg:
        return 0

    if msg.startswith("chore: version ") or msg.startswith("chore(release):"):
        error("Cannot amend CI version commit. Make a new commit first.")
        return 1
    return 0


def _get_last_commit_message(*, cwd: str | None) -> str | None:
    """Get the last commit's full message."""
    result = run_cmd(
        ["git", "log", "-1", "--format=%B"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _run_check(*, cwd: str | None) -> int:
    """Run hyperi-ci check. Returns exit code."""
    from hyperi_ci.dispatch import run_stage

    dir_path = Path(cwd) if cwd else None
    for stage in ("quality", "test"):
        rc = run_stage(stage, project_dir=dir_path, local=True)
        if rc != 0:
            return rc
    return 0


def _rebase_and_push(
    *,
    branch: str | None = None,
    cwd: str | None = None,
) -> int:
    """Pull --rebase then push with HYPERCI_PUSH=1."""
    rebase_cmd = ["git", "pull", "--rebase"]
    if branch:
        rebase_cmd.extend(["origin", branch])

    try:
        run_cmd(rebase_cmd, cwd=cwd)
    except subprocess.CalledProcessError:
        error("Rebase failed — resolve conflicts and try again")
        return 1

    return _push_with_env(cwd=cwd)


def _push_with_env(
    *,
    args: list[str] | None = None,
    cwd: str | None = None,
) -> int:
    """Run git push with HYPERCI_PUSH=1 set."""
    cmd = ["git", "push"]
    if args:
        cmd.extend(args)

    try:
        run_cmd(cmd, env={"HYPERCI_PUSH": "1"}, cwd=cwd)
    except subprocess.CalledProcessError:
        error("Push failed")
        return 1

    success("Pushed successfully")
    return 0


def _get_current_tags(*, cwd: str | None) -> set[str]:
    """Snapshot of current version tags."""
    result = run_cmd(
        ["git", "tag", "--list", "v*"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        return set()
    return {t.strip() for t in result.stdout.splitlines() if t.strip()}


def _detect_new_tag(before: set[str], *, cwd: str | None) -> str | None:
    """Fetch tags and return the new tag if one was created."""
    run_cmd(["git", "fetch", "--tags"], check=False, capture=True, cwd=cwd)

    after = _get_current_tags(cwd=cwd)
    new_tags = after - before

    if not new_tags:
        return None

    # Sort by version descending, return latest
    sorted_tags = sorted(new_tags, key=_version_sort_key, reverse=True)
    return sorted_tags[0]


def _version_sort_key(tag: str) -> tuple[int, ...]:
    """Parse a version tag like v1.2.3 into a sortable tuple."""
    stripped = tag.lstrip("v")
    parts: list[int] = []
    for part in stripped.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _get_latest_run_id(branch: str) -> str | None:
    """Get the latest CI run ID for a branch."""
    run = get_latest_run(branch=branch)
    if run and run.get("databaseId"):
        return str(run["databaseId"])
    return None


def _poll_for_new_run(
    branch: str,
    previous_run_id: str | None,
    timeout: int = 30,
) -> str | None:
    """Poll until a new CI run appears (different from previous_run_id).

    Args:
        branch: Branch to check.
        previous_run_id: Run ID before push (to detect new run).
        timeout: Maximum seconds to wait.

    Returns:
        New run ID, or None if timeout.

    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        current = _get_latest_run_id(branch)
        if current and current != previous_run_id:
            return current
    return None
