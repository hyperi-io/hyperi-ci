# Project:   HyperI CI
# File:      src/hyperi_ci/push.py
# Purpose:   Push wrapper with pre-checks and meta-operations
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Push wrapper with pre-checks and meta-operations.

Wraps git push with:

- Pre-push validation (``hyperi-ci check``)
- Auto-rebase to sync semantic-release commits
- ``--publish`` (alias ``--release``): amend HEAD with the
  ``Publish: true`` git trailer before pushing. The single CI run
  triggered by the push runs through the version-first pipeline and
  produces the tag + registry uploads in one shot.
- ``--no-ci``: amend last commit with ``[skip ci]`` marker

All flows set ``HYPERCI_PUSH=1`` so the pre-push hook allows the push.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.gh import get_current_branch, require_gh


PUBLISH_TRAILER_KEY = "Publish"
PUBLISH_TRAILER_VALUE = "true"


def push(
    *,
    publish: bool = False,
    no_ci: bool = False,
    dry_run: bool = False,
    force: bool = False,
    project_dir: Path | None = None,
) -> int:
    """Push with pre-checks and optional meta-operations.

    Args:
        publish: Stamp the head commit with the ``Publish: true``
            trailer (justified amend) and push. The CI run sees the
            trailer, predicts the next version, stamps it into
            Cargo.toml/VERSION before build, then tags + publishes in
            the same workflow run.
        no_ci: Amend last commit with ``[skip ci]`` and push.
        dry_run: Show what would happen without executing.
        force: Skip hyperi-ci check step.
        project_dir: Project directory (default: cwd).

    Returns:
        Exit code: 0=success, non-zero=failure.
    """
    if publish and no_ci:
        error("--publish and --no-ci are mutually exclusive")
        return 1

    cwd = str(project_dir) if project_dir else None

    if no_ci:
        return _skip_ci_push(dry_run=dry_run, cwd=cwd)

    if publish:
        return _publish_push(dry_run=dry_run, force=force, cwd=cwd)

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


def _publish_push(*, dry_run: bool, force: bool, cwd: str | None) -> int:
    """Stamp HEAD with the Publish: true trailer, then push.

    The CI workflow detects the trailer in setup, runs semantic-release
    in --dry-run to predict the next version, stamps that version into
    Cargo.toml/VERSION before build, and after build runs
    semantic-release for real (creating the tag) plus the publish
    stage. One run, one tag, one publish — no chained dispatches.

    The amend is justified because:
    - HEAD is the user's own unpushed commit (we just verified the
      working tree is clean).
    - The user explicitly opted in via --publish.
    - We're adding a trailer, not rewriting the message body.

    The previous "_release_push" model push-then-watch-then-dispatched
    a second workflow run. That doubled CI time (build runs twice,
    once at the old version) and was the entire reason for the
    version-first refactor.
    """
    if not require_gh():
        return 1

    branch = get_current_branch()
    if branch != "main":
        error("--publish only works from main")
        return 1

    if rc := _check_dirty_tree(cwd=cwd):
        return rc

    if not force:
        if rc := _run_check(cwd=cwd):
            return rc

    head_msg = _get_last_commit_message(cwd=cwd)
    if not head_msg:
        error("Could not read HEAD commit message")
        return 1

    if _has_publish_trailer(head_msg):
        info("HEAD already carries Publish: true trailer — pushing as-is")
    else:
        if dry_run:
            info("Dry run: would amend HEAD to add 'Publish: true' trailer, then push")
            return 0
        rc = _amend_publish_trailer(cwd=cwd)
        if rc != 0:
            return rc

    if dry_run:
        info("Dry run: would rebase and push")
        return 0

    rc = _rebase_and_push(branch="main", cwd=cwd)
    if rc != 0:
        return rc

    info("Pushed. The CI run will tag + publish in a single workflow.")
    info("Watch: hyperi-ci watch")
    return 0


def _has_publish_trailer(message: str) -> bool:
    """True iff the commit message already has ``Publish: true``."""
    for line in message.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            if (
                key.strip().lower() == PUBLISH_TRAILER_KEY.lower()
                and value.strip().lower() == PUBLISH_TRAILER_VALUE
            ):
                return True
    return False


def _amend_publish_trailer(*, cwd: str | None) -> int:
    """Amend HEAD to add the Publish: true trailer (no message change)."""
    try:
        run_cmd(
            [
                "git",
                "commit",
                "--amend",
                "--no-edit",
                "--trailer",
                f"{PUBLISH_TRAILER_KEY}: {PUBLISH_TRAILER_VALUE}",
            ],
            cwd=cwd,
            capture=True,
        )
    except subprocess.CalledProcessError as exc:
        error(f"Failed to amend HEAD with Publish: true trailer: {exc}")
        return 1
    info(f"Amended HEAD with `{PUBLISH_TRAILER_KEY}: {PUBLISH_TRAILER_VALUE}` trailer")
    return 0


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


