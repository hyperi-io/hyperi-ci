# Project:   HyperI CI
# File:      src/hyperi_ci/release/dispatch.py
# Purpose:   Retroactive release via workflow_dispatch on existing tag
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Trigger a release through workflow_dispatch: from HEAD, or on an existing tag.

The usual path is `hyperi-ci push --release`. This one retries a failed
publish without re-tagging, or releases HEAD without a marker commit.
"""

import subprocess

from hyperi_ci.common import (
    error,
    explicit_version,
    info,
    latest_version_tag,
    success,
    warn,
)

# A consumer's `on.workflow_dispatch.inputs` must declare all of these or the
# dispatch fails with HTTP 422 (issue #88); `hyperi-ci audit-callers` checks it.
DISPATCH_INPUTS: tuple[str, ...] = ("tag", "from-head", "bump")


def _dispatch_cmd(workflow: str, inputs: dict[str, str]) -> list[str]:
    """Build a `gh workflow run` command, rejecting inputs outside DISPATCH_INPUTS."""
    unknown = sorted(set(inputs) - set(DISPATCH_INPUTS))
    if unknown:
        raise ValueError(
            f"dispatch input(s) {unknown} not in DISPATCH_INPUTS -- add them "
            f"there so audit-callers requires consumers to declare them"
        )
    cmd = ["gh", "workflow", "run", workflow]
    for key, value in inputs.items():
        cmd += ["-f", f"{key}={value}"]
    return cmd


def _get_version_tags() -> list[str]:
    """Get all version tags sorted by version descending."""
    result = subprocess.run(
        ["git", "tag", "--list", "v*", "--sort=-version:refname"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def _get_tag_info(tag: str) -> str:
    """Get tag date and commit summary."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%ci", tag],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
        encoding="utf-8",
        errors="replace",
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
    """Return the highest final-release tag, or None when the repo carries none.

    Prereleases are skipped because ``-v:refname`` sorts ``v1.1.2-beta.1``
    above ``v1.1.1``.
    """
    version = latest_version_tag()
    return f"v{version}" if version else None


def _head_in_sync_with_origin() -> bool:
    """Return True if local HEAD matches origin/main, the commit the CI tags."""
    local = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    remote = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if local.returncode != 0 or remote.returncode != 0:
        return True  # can't tell -- don't block
    return local.stdout.strip() == remote.stdout.strip()


def dispatch_from_head(*, bump: str = "auto", dry_run: bool = False) -> int:
    """Dispatch a release of main HEAD; the runner versions, tags and publishes.

    ``bump`` is ``auto`` (semantic-release decides, and may no-op), ``patch``
    or ``minor`` (force a release), or an explicit ``X.Y.Z`` that tags HEAD
    at exactly that version, skipping a taken or orphaned tag (issue #37).
    """
    explicit = explicit_version(bump)
    if explicit is None and bump not in ("auto", "patch", "minor"):
        error(
            f"Invalid version/bump '{bump}' -- expected auto, patch, minor, "
            "or an explicit X.Y.Z version"
        )
        return 1
    if explicit is not None:
        bump = explicit  # normalised (no leading 'v') -- travels in the bump input

    if not _head_in_sync_with_origin():
        warn(
            "Local HEAD differs from origin/main -- the CI tags origin/main "
            "HEAD. Push your commits first, or expect to release what's on "
            "the remote."
        )

    workflow = _detect_workflow_file()
    cmd = _dispatch_cmd(workflow, {"from-head": "true", "bump": bump})

    label = f"version=v{bump}" if explicit is not None else f"bump={bump}"

    if dry_run:
        info(f"Would run: {' '.join(cmd)}")
        return 0

    info(f"Dispatching from-head publish ({label}) via {workflow}...")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        error("Failed to dispatch workflow")
        return result.returncode

    success(f"Release dispatched from HEAD ({label})")
    info("The CI will resolve the version, tag HEAD, and publish.")
    info("Watch progress: hyperi-ci watch")
    return 0


def dispatch_publish(tag: str, dry_run: bool = False) -> int:
    """Re-dispatch a publish for an existing tag; "latest" picks the newest.

    An existing GH Release does not block: the publish handlers skip
    artefacts already in their registry, so a retry fills in what a partial
    publish missed (issue #35).
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
            "(no tag) -- the CI will create the tag."
        )
        info("Available tags:")
        for t in tags[:10]:
            info(f"  {t}")
        return 1

    if _tag_has_release(tag):
        warn(
            f"GH Release already exists for {tag} -- re-dispatching to fill "
            "any registries a partial publish missed (publish is idempotent)."
        )

    workflow = _detect_workflow_file()
    cmd = _dispatch_cmd(workflow, {"tag": tag})

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
