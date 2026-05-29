# Project:   HyperI CI
# File:      src/hyperi_ci/publish/dispatch.py
# Purpose:   Retroactive publish via workflow_dispatch on existing tag
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Retroactive publish: workflow_dispatch on an existing tag.

The primary publish path is now `hyperi-ci push --publish` (single CI
run, version-first pipeline gated by the ``Publish: true`` commit
trailer). This module covers the secondary "I want to re-publish an
existing tag" use case — e.g. a previous publish run failed mid-way and
needs retrying without re-tagging.

Lists unpublished version tags and triggers the workflow_dispatch event
for a specific tag.
"""

from __future__ import annotations

import subprocess

from hyperi_ci.common import error, info, success, warn


def _get_version_tags() -> list[str]:
    """Get all version tags sorted by version descending."""
    result = subprocess.run(
        ["git", "tag", "--list", "v*", "--sort=-version:refname"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [t.strip() for t in result.stdout.splitlines() if t.strip()]


def _tag_has_release(tag: str) -> bool:
    """Check if a GH Release exists for this tag."""
    result = subprocess.run(
        ["gh", "release", "view", tag],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _get_tag_info(tag: str) -> str:
    """Get tag date and commit summary."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%ci", tag],
        capture_output=True,
        text=True,
    )
    date = result.stdout.strip()[:10] if result.returncode == 0 else "unknown"
    return date


def list_unpublished() -> int:
    """List version tags that don't have a GH Release."""
    tags = _get_version_tags()
    if not tags:
        info("No version tags found")
        return 0

    unpublished: list[tuple[str, str]] = []
    for tag in tags[:20]:
        if not _tag_has_release(tag):
            date = _get_tag_info(tag)
            unpublished.append((tag, date))

    if not unpublished:
        info("All recent tags have GH Releases")
        return 0

    info("Unpublished version tags:")
    for tag, date in unpublished:
        info(f"  {tag}  ({date})")

    return 0


def _detect_workflow_file() -> str:
    """Detect the CI workflow filename from the repo."""
    result = subprocess.run(
        ["gh", "workflow", "list", "--json", "name,id"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "ci.yml"

    import json

    try:
        workflows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "ci.yml"

    for wf in workflows:
        name = wf.get("name", "").lower()
        if name in ("ci", "rust ci", "python ci", "go ci", "typescript ci"):
            return "ci.yml"

    return "ci.yml"


def resolve_latest_tag() -> str | None:
    """Resolve the latest version tag."""
    tags = _get_version_tags()
    return tags[0] if tags else None


def _head_in_sync_with_origin() -> bool:
    """True if local HEAD == origin/main — the commit the CI will tag.

    from-head dispatch tags `origin/main` HEAD on the runner, not the
    local tree. If the operator's HEAD differs, what gets released is not
    what they're looking at — warn so there are no surprises.
    """
    local = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    remote = subprocess.run(
        ["git", "rev-parse", "origin/main"], capture_output=True, text=True
    )
    if local.returncode != 0 or remote.returncode != 0:
        return True  # can't tell — don't block
    return local.stdout.strip() == remote.stdout.strip()


def dispatch_from_head(*, bump: str = "auto", dry_run: bool = False) -> int:
    """Release/retry the current `main` HEAD — the CI creates the tag.

    This is the first-class "I need to release/retry that" path (issue #35).
    The CLI only *triggers* the workflow; the runner resolves the version,
    creates the tag at HEAD, and publishes — so there is no artificial
    `fix:` commit and no local tag push. `bump=auto` lets semantic-release
    pick the version from commits (no-ops if nothing is release-worthy);
    `bump=patch|minor` forces a release regardless.
    """
    if bump not in ("auto", "patch", "minor"):
        error(f"Invalid bump '{bump}' — expected auto, patch, or minor")
        return 1

    if not _head_in_sync_with_origin():
        warn(
            "Local HEAD differs from origin/main — the CI tags origin/main "
            "HEAD. Push your commits first, or expect to release what's on "
            "the remote."
        )

    workflow = _detect_workflow_file()
    cmd = [
        "gh",
        "workflow",
        "run",
        workflow,
        "-f",
        "from-head=true",
        "-f",
        f"bump={bump}",
    ]

    if dry_run:
        info(f"Would run: {' '.join(cmd)}")
        return 0

    info(f"Dispatching from-head publish (bump={bump}) via {workflow}...")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        error("Failed to dispatch workflow")
        return result.returncode

    success(f"Release dispatched from HEAD (bump={bump})")
    info("The CI will resolve the version, tag HEAD, and publish.")
    info("Watch progress: hyperi-ci watch")
    return 0


def dispatch_publish(tag: str, dry_run: bool = False) -> int:
    """Re-dispatch a publish for an EXISTING tag (idempotent retry).

    If tag is "latest", resolves to the most recent version tag. A GH
    Release that already exists no longer blocks — the publish handlers
    skip artefacts already in their registry ("already exists"), so a
    retry safely fills in whatever a partial publish missed (issue #35).
    """
    if tag == "latest":
        resolved = resolve_latest_tag()
        if not resolved:
            error("No version tags found")
            return 1
        info(f"Resolved 'latest' to {resolved}")
        tag = resolved

    tags = _get_version_tags()
    if tag not in tags:
        error(f"Tag '{tag}' does not exist")
        info(
            "To release the current HEAD instead, run `hyperi-ci publish` "
            "(no tag) — the CI will create the tag."
        )
        info("Available tags:")
        for t in tags[:10]:
            info(f"  {t}")
        return 1

    if _tag_has_release(tag):
        warn(
            f"GH Release already exists for {tag} — re-dispatching to fill "
            "any registries a partial publish missed (publish is idempotent)."
        )

    workflow = _detect_workflow_file()
    cmd = ["gh", "workflow", "run", workflow, "-f", f"tag={tag}"]

    if dry_run:
        info(f"Would run: {' '.join(cmd)}")
        return 0

    info(f"Dispatching publish for {tag} via {workflow}...")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        error("Failed to dispatch workflow")
        return result.returncode

    success(f"Publish dispatched for {tag}")
    info("Watch progress: hyperi-ci watch")
    return 0
