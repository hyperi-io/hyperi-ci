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

# Bump → conventional-commits type that semantic-release will treat as
# the corresponding semver bump. We deliberately exclude "major" — major
# bumps require a human to write `BREAKING CHANGE:` in the commit body
# (per HyperI commit-type discipline). Forcing a major via flag would
# bypass that gate.
_BUMP_TO_TYPE: dict[str, str] = {
    "patch": "fix",
    "minor": "feat",
}


def push(
    *,
    publish: bool = False,
    no_ci: bool = False,
    bump: str | None = None,
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
        bump: ``"patch"`` or ``"minor"`` — force a release-worthy commit
            on top of HEAD even when the actual commits are no-bump
            (e.g. ``docs:`` only). Implies ``--publish``. Lets you
            release a docs-only or refactor-only PR without manually
            adding a fake ``fix:`` commit. Major bumps are deliberately
            excluded — they require a human-written ``BREAKING CHANGE:``
            footer per HyperI commit-type discipline.
        dry_run: Show what would happen without executing.
        force: Skip hyperi-ci check step.
        project_dir: Project directory (default: cwd).

    Returns:
        Exit code: 0=success, non-zero=failure.
    """
    if publish and no_ci:
        error("--publish and --no-ci are mutually exclusive")
        return 1
    if bump and no_ci:
        error("--bump-* and --no-ci are mutually exclusive")
        return 1
    if bump and bump not in _BUMP_TO_TYPE:
        error(
            f"Unknown bump level {bump!r}. Use 'patch' or 'minor'. "
            f"Major bumps require a human-written BREAKING CHANGE: footer."
        )
        return 1

    cwd = str(project_dir) if project_dir else None

    if no_ci:
        return _skip_ci_push(dry_run=dry_run, cwd=cwd)

    # --bump-* implies --publish (you can't bump without publishing)
    if publish or bump:
        return _publish_push(
            dry_run=dry_run, force=force, bump=bump, cwd=cwd
        )

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


def _publish_push(
    *,
    dry_run: bool,
    force: bool,
    bump: str | None,
    cwd: str | None,
) -> int:
    """Mark HEAD as a publish run, then push.

    Two paths:

    - Default (``bump=None``): the user's HEAD commit IS release-worthy
      (a ``fix:``/``feat:``/``perf:``/``hotfix:``/``security:`` commit).
      We amend HEAD with the ``Publish: true`` trailer to signal the
      workflow to tag + publish.

    - Forced bump (``bump="patch"`` or ``"minor"``): the user's HEAD
      commits are NOT release-worthy (e.g. docs-only) but they want a
      release anyway. We add a NEW empty commit on top with a
      conventional-commit message that semantic-release will analyse
      as a patch/minor — plus the ``Publish: true`` trailer. Honest
      git history: the marker commit explicitly states "this is a
      forced release" rather than smuggling a fake fix into source.

    Either way, the resulting CI run goes through the version-first
    pipeline: predict → stamp → build → tag + publish in one workflow.
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

    if bump:
        # Forced bump: add an empty release-marker commit.
        commit_type = _BUMP_TO_TYPE[bump]
        marker_subject = f"{commit_type}(release): force {bump} bump"
        marker_message = (
            f"{marker_subject}\n\n"
            f"Forced {bump} release requested via `hyperi-ci push --bump-{bump}`.\n"
            f"The preceding commits don't independently warrant a {bump} bump\n"
            f"under conventional-commits rules; this empty marker commit\n"
            f"records the operator's explicit decision to publish anyway.\n"
            f"\n"
            f"{PUBLISH_TRAILER_KEY}: {PUBLISH_TRAILER_VALUE}\n"
        )
        if dry_run:
            info(
                f"Dry run: would add empty release-marker commit "
                f"`{marker_subject}` (with Publish: true trailer), then push"
            )
            return 0
        rc = _add_release_marker_commit(message=marker_message, cwd=cwd)
        if rc != 0:
            return rc
        info(f"Added empty release-marker: `{marker_subject}`")
    else:
        head_msg = _get_last_commit_message(cwd=cwd)
        if not head_msg:
            error("Could not read HEAD commit message")
            return 1

        if _has_publish_trailer(head_msg):
            info("HEAD already carries Publish: true trailer — pushing as-is")
        else:
            if dry_run:
                info(
                    "Dry run: would amend HEAD to add 'Publish: true' trailer, "
                    "then push"
                )
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


def _add_release_marker_commit(*, message: str, cwd: str | None) -> int:
    """Create an empty ``fix(release):``/``feat(release):`` marker commit.

    Used by ``--bump-patch`` / ``--bump-minor`` to give semantic-release
    a release-worthy commit to analyse without requiring the user to
    invent a fake source change. The marker IS a real commit in git
    history with a clear, conventional message — not a code-side
    artificial change.
    """
    try:
        run_cmd(
            [
                "git",
                "commit",
                "--allow-empty",
                "-m",
                message,
            ],
            cwd=cwd,
            capture=True,
        )
    except subprocess.CalledProcessError as exc:
        error(f"Failed to create release-marker commit: {exc}")
        return 1
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
    # `--allow-empty` covers the edge case where HEAD is already an empty
    # commit (e.g. an empty `chore: trigger` marker) — git refuses to
    # amend an empty commit by default. The trailer-only amend doesn't
    # add content, so without --allow-empty the amend fails. Including
    # the flag is harmless when there IS content.
    try:
        run_cmd(
            [
                "git",
                "commit",
                "--amend",
                "--no-edit",
                "--allow-empty",
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
