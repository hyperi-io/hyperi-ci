# Project:   HyperI CI
# File:      src/hyperi_ci/release/oracle.py
# Purpose:   Release-based version oracle (issue #31 Phase 2a)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Compute the next version from the last GitHub release + conventional commits.

Replaces semantic-release's git-tag-reachability dry-run. Reading the last
version from the Releases API (not "tags reachable from HEAD") lets release
tags live off-main — the enabler for the frozen-graph fix (#31) — and makes
version computation immune to orphaned/moved tags.

Pure logic (`commit_bump`/`max_bump`/`bump_version`/`compute_next_version`) is
unit-tested; the I/O wrappers shell out to `gh` + `git`.
"""

from __future__ import annotations

import json
import re
import subprocess

# Conventional-commit type → bump. SSoT mirror of the releaseRules in
# .github/actions/setup-semantic-release/default.releaserc.json (an anti-drift
# test asserts they match while semantic-release coexists; #31 Phase 2d retires
# the JSON). `breaking` (`!` or BREAKING CHANGE) → major is handled in
# commit_bump, not here.
RELEASE_RULES: dict[str, str | None] = {
    "feat": "minor",
    "fix": "patch",
    "perf": "patch",
    "sec": "patch",
    "hotfix": "patch",
    "security": "patch",
    "docs": None,
    "test": None,
    "refactor": None,
    "style": None,
    "build": None,
    "ci": None,
    "chore": None,
    "deps": None,
    "revert": None,
    "wip": None,
    "cleanup": None,
    "data": None,
    "debt": None,
    "design": None,
    "infra": None,
    "meta": None,
    "ops": None,
    "review": None,
    "spike": None,
    "ui": None,
}

_BUMP_RANK = {None: 0, "patch": 1, "minor": 2, "major": 3}
_HEADER = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\([^)]*\))?(?P<bang>!)?:")
_BREAKING = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)


def commit_bump(
    message: str, rules: dict[str, str | None] = RELEASE_RULES
) -> str | None:
    """Bump implied by one commit message, or None for no release.

    `!` after the type/scope or a `BREAKING CHANGE:` footer → major, regardless
    of type. Otherwise the type maps via `rules`. Non-conventional or unknown
    types → None.
    """
    if not message:
        return None
    header = message.splitlines()[0]
    m = _HEADER.match(header)
    if (m and m.group("bang")) or _BREAKING.search(message):
        return "major"
    if not m:
        return None
    return rules.get(m.group("type").lower())


def max_bump(bumps: list[str | None]) -> str | None:
    """Highest-precedence bump (major > minor > patch > None)."""
    best: str | None = None
    for b in bumps:
        if _BUMP_RANK[b] > _BUMP_RANK[best]:
            best = b
    return best


def bump_version(version: str, bump: str) -> str:
    """Apply a semver bump to `version` (leading 'v' tolerated)."""
    major, minor, patch = (int(x) for x in version.removeprefix("v").split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor}.{patch}"


def compute_next_version(
    last_version: str | None,
    commit_messages: list[str],
    rules: dict[str, str | None] = RELEASE_RULES,
) -> str | None:
    """Next version, or None when no commit is release-worthy.

    `last_version` None means no prior release → first release is 1.0.0 (matches
    semantic-release) when any commit is release-worthy.
    """
    bump = max_bump([commit_bump(m, rules) for m in commit_messages])
    if bump is None:
        return None
    if last_version is None:
        return "1.0.0"
    return bump_version(last_version, bump)


# ---------------------------------------------------------------------------
# Version source: the highest pure-semver TAG across all tags.
#
# Not "tags reachable from HEAD" (that coupling caused the orphaning bug AND
# blocks off-main frozen-graph tags, #31) and not the GitHub Releases API
# alone (hyperi-ci's own releases lag its tags). The highest semver tag is
# decoupled from reachability, immune to orphaning (an orphaned tag is still
# counted, so next always exceeds it), and survives off-main release commits.
# Read via the GitHub API so the runner's checkout depth is irrelevant.
# ---------------------------------------------------------------------------

_SEMVER_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_semver(tag: str) -> tuple[int, int, int] | None:
    """Parse `v1.2.3`/`1.2.3` → tuple. None for prereleases/suffixed/non-semver."""
    m = _SEMVER_TAG.match(tag.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def highest_release(tags: list[str]) -> str | None:
    """Highest stable semver among `tags` (without leading v). None if none."""
    best: tuple[int, int, int] | None = None
    for t in tags:
        ver = parse_semver(t)
        if ver and (best is None or ver > best):
            best = ver
    return ".".join(map(str, best)) if best else None


def _repo() -> str:
    import os

    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _gh_json(path: str) -> object | None:
    try:
        result = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None


def _all_tags(repo: str) -> list[str]:
    data = _gh_json(f"/repos/{repo}/tags?per_page=100")
    return [t.get("name", "") for t in data] if isinstance(data, list) else []


def _commits_since(repo: str, base: str | None, head: str) -> list[str]:
    """Commit messages on `head` since tag `base` (all of head if base is None).

    Uses the compare API — `base...head` returns commits unique to head since
    their merge-base, which is correct even when the previous release tag is an
    off-main frozen-graph commit (its merge-base with head is main HEAD).
    """
    if base is None:
        data = _gh_json(f"/repos/{repo}/commits?sha={head}&per_page=100")
        commits = data if isinstance(data, list) else []
    else:
        data = _gh_json(f"/repos/{repo}/compare/{base}...{head}")
        commits = data.get("commits", []) if isinstance(data, dict) else []
    return [c.get("commit", {}).get("message", "") for c in commits]


def resolve_next_version(head: str | None = None) -> str | None:
    """Next version for `head` (default current HEAD SHA), or None if no
    release-worthy commit since the highest existing release."""
    import os

    repo = _repo()
    head = head or os.environ.get("GITHUB_SHA") or "HEAD"
    last = highest_release(_all_tags(repo))
    base = f"v{last}" if last else None
    commits = _commits_since(repo, base, head)
    return compute_next_version(last, commits)
