# Project:   HyperI CI
# File:      src/hyperi_ci/seed.py
# Purpose:   Create a repo's first version tag, once, at adoption
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Give a tag-less repo the one thing the whole version pipeline reads.

Every version decision here starts from the latest ``v*`` tag. A repo with
no tags at all has nothing to start from, and the old answer was to read the
committed ``VERSION`` file — the assumption issue #85 removes. The new answer
is to create the tag once, at adoption, from the version the project already
declares about itself.

The seed tag is a STARTING MARKER, not a release: it says "this is where the
history begins", and the first published release bumps from it. That keeps
tag-on-publish honest — no seed tag is ever created for a version this tool
published, because the publish path creates its own.

Idempotent by construction: a repo with any ``v*`` tag already has its truth,
and seeding refuses rather than adding a second opinion.
"""

from __future__ import annotations

from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success
from hyperi_ci.version_source import seed_version


def existing_version_tags(cwd: Path | None = None) -> list[str]:
    """Every ``v*`` tag in the repo, newest semver first (empty if none)."""
    result = run_cmd(
        ["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"],
        capture=True,
        check=False,
        cwd=str(cwd) if cwd else None,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def seed_tag(
    *,
    project_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Create the repo's first ``v*`` tag from its declared version.

    Args:
        project_dir: Project root. Defaults to cwd.
        dry_run: Report what would be tagged, create nothing.

    Returns:
        0 when the repo ends up with a version tag (created, or already had
        one), 1 when the repo is not usable — no git repo, or no commit to
        tag.

    """
    root = project_dir or Path.cwd()
    cwd = str(root)

    inside = run_cmd(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if inside.returncode != 0:
        error(f"seed-tag: {root} is not a git repository")
        return 1

    head = run_cmd(
        ["git", "rev-parse", "--verify", "HEAD"], capture=True, check=False, cwd=cwd
    )
    if head.returncode != 0 or not head.stdout.strip():
        error("seed-tag: no commits yet — commit something before seeding a version")
        return 1

    existing = existing_version_tags(root)
    if existing:
        info(f"seed-tag: {existing[0]} already exists — nothing to seed")
        return 0

    version, source = seed_version(root)
    tag = f"v{version}"
    origin = f"declared in {source}" if source != "default" else "greenfield default"

    if dry_run:
        info(f"seed-tag: would create {tag} at HEAD ({origin})")
        return 0

    created = run_cmd(
        [
            "git",
            "tag",
            "-a",
            tag,
            "-m",
            f"Seed version {tag}\n\n"
            f"Starting point for hyperi-ci's version pipeline, {origin}.\n"
            f"Not a published release — the first release bumps from here.\n",
        ],
        check=False,
        cwd=cwd,
    )
    if created.returncode != 0:
        error(f"seed-tag: git tag {tag} failed")
        return 1

    success(f"seed-tag: created {tag} at HEAD ({origin})")
    info(f"Push it with: git push origin {tag}")
    return 0
