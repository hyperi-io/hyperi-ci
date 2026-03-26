# Project:   HyperI CI
# File:      src/hyperi_ci/release.py
# Purpose:   Tag-based publish dispatch (replaces release-merge)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Publish dispatch via workflow_dispatch.

Lists unpublished version tags and triggers publish workflows for specific
tags. Replaces the old release-merge flow (no release branch needed).
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


def dispatch_publish(tag: str, dry_run: bool = False) -> int:
    """Trigger a publish workflow for the given tag."""
    tags = _get_version_tags()
    if tag not in tags:
        error(f"Tag '{tag}' does not exist")
        info("Available tags:")
        for t in tags[:10]:
            info(f"  {t}")
        return 1

    if _tag_has_release(tag):
        warn(f"GH Release already exists for {tag}")
        info("Use 'gh release delete' first if you want to re-publish")
        return 1

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
