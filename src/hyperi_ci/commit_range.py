# Project:   HyperI CI
# File:      src/hyperi_ci/commit_range.py
# Purpose:   Resolve the commit range a CI event introduced, and its bump
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve the commits a CI event introduced, and whether they ship a release.

Shared by ``hyperi_ci.quality.commit_validation`` (validates every message in
the range) and the ``predict-version`` composite (is the range release-worthy,
and what sits unreleased on a validate-only run).

Stdlib-only, importing nothing from the package except
:mod:`hyperi_ci.release_rules`, because the composite loads it BY PATH where
hyperi-ci is not installed. An import of ``common`` or ``config`` here breaks
the plan job.
"""

import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

from hyperi_ci.release_rules import classify_commit, load_type_bump

_COMMIT_SEPARATOR = "----END----"
_COMMIT_FMT = f"%H%n%s%n%b%n{_COMMIT_SEPARATOR}"

# Highest bump first, for rendering the mix in a warning.
_BUMP_RENDER_ORDER = ("major", "minor", "patch")
_SECONDS_PER_DAY = 86400


def _parse_git_log(output: str) -> list[tuple[str, str]]:
    commits = []
    for block in output.split(_COMMIT_SEPARATOR):
        block = block.strip()
        if not block:
            continue
        first_newline = block.index("\n")
        commit_hash = block[:first_newline].strip()
        full_msg = block[first_newline:].strip()
        if commit_hash and full_msg:
            commits.append((commit_hash, full_msg))
    return commits


def git_log(args: list[str]) -> tuple[int, list[tuple[str, str]]]:
    """Run ``git log --pretty=<fmt> <args>``; return ``(returncode, commits)``."""
    result = subprocess.run(
        ["git", "log", f"--pretty={_COMMIT_FMT}", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return result.returncode, []
    return 0, _parse_git_log(result.stdout)


def is_zero_sha(sha: str) -> bool:
    """Return True for git's all-zeros sentinel SHA (branch creation / no parent)."""
    return len(sha) >= 7 and set(sha) == {"0"}


def event_payload() -> dict:
    """Return the Actions event payload, or ``{}`` when there is none to read."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def commits_in_range() -> tuple[list[tuple[str, str]], bool]:
    """Return ``(commits, resolved)`` for the commits this CI event introduced.

    ``resolved`` is True when the range was determined authoritatively, even if
    empty. It is False when it could not be (shallow checkout, detached HEAD,
    missing ``before`` commit), and the caller MUST treat an empty result then
    as a degraded backstop, not success (issue #52).

    Resolution, in order of authority:

    1. ``push`` -> ``before..after`` from the event payload. After a push to a
       tracked branch ``origin/<branch>`` already equals HEAD, so
       ``origin/main..HEAD`` would be empty and validate nothing. This range
       also covers merge-imported history.
    2. ``pull_request`` -> ``<base sha>..HEAD``.
    3. ``merge_group`` -> ``base_sha..head_sha`` from the payload, which is the
       squash commit the queue will fast-forward main to.
    4. Local / unknown contexts: ``origin/main..HEAD``, then ``HEAD~20..HEAD``.

    A resolved-but-empty range returns ``([], True)`` without falling through.
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    payload = event_payload()

    if event == "push":
        before = str(payload.get("before", ""))
        after = str(payload.get("after", "")) or "HEAD"
        # A real prior tip gives the authoritative range.
        if before and not is_zero_sha(before):
            rc, commits = git_log([f"{before}..{after}"])
            if rc == 0:
                return commits, True
            # `before` is missing from a shallow clone. Falling through to
            # origin/main..HEAD would report an empty range (issue #52).
            return [], False
        # Branch creation (all-zeros before): fall through to the generic ranges.
    elif event == "pull_request":
        base = str((payload.get("pull_request") or {}).get("base", {}).get("sha", ""))
        if not base and os.environ.get("GITHUB_BASE_REF"):
            base = f"origin/{os.environ['GITHUB_BASE_REF']}"
        if base:
            rc, commits = git_log([f"{base}..HEAD"])
            if rc == 0:
                return commits, True
    elif event == "merge_group":
        group = payload.get("merge_group") or {}
        base = str(group.get("base_sha", ""))
        head = str(group.get("head_sha", "")) or "HEAD"
        if base:
            rc, commits = git_log([f"{base}..{head}"])
            if rc == 0:
                return commits, True
            # As for push: a fallback range would validate the wrong set.
            return [], False

    for git_range in ("origin/main..HEAD", "HEAD~20..HEAD"):
        rc, commits = git_log([git_range])
        if rc == 0:
            return commits, True

    return [], False


def is_release_worthy(project_dir: Path | None = None) -> tuple[bool, str]:
    """Return ``(worthy, reason)`` for the commits this CI event introduced.

    Worthy means at least one commit resolves to a bump other than ``none``
    under :mod:`hyperi_ci.release_rules`.

    An UNRESOLVABLE range returns True so a shallow clone cannot silently skip
    the quality and test gate (no silent skips). A resolved-but-empty range
    returns False.
    """
    commits, resolved = commits_in_range()
    if not resolved:
        return True, "could not resolve the pushed range -- running the checks"
    if not commits:
        return False, "no commits in the pushed range"

    type_bump = load_type_bump(project_dir if project_dir is not None else Path.cwd())
    for commit_hash, message in commits:
        bump = classify_commit(message, type_bump)
        if bump != "none":
            subject = message.splitlines()[0] if message else ""
            return True, f"{commit_hash[:8]} is a {bump} bump: {subject}"

    return False, f"no release-worthy commit in {len(commits)} pushed commit(s)"


def _last_version_tag() -> str | None:
    """Return the nearest ``v*`` tag reachable from HEAD, or None if there is none."""
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _tag_age_days(tag: str) -> int | None:
    """Return whole days since ``tag``'s commit, or None if git could not say."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%ct", tag],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    try:
        committed = int(result.stdout.strip())
    except ValueError:
        return None
    return max(0, int((time.time() - committed) // _SECONDS_PER_DAY))


def unreleased_since_tag(
    project_dir: Path | None = None,
) -> tuple[str | None, list[tuple[str, str]]]:
    """Return the nearest ``v*`` tag and the releasable commits HEAD holds past it.

    Cumulative, not per-push: every releasable commit the last tag lacks.

    Args:
        project_dir: Directory whose ``.releaserc.json`` overrides the bump
            map. Defaults to the current directory.

    Returns:
        ``(tag, commits)`` where ``commits`` is ``(sha, bump)`` for each
        releasable commit in ``tag..HEAD``. ``(None, [])`` when no ``v*`` tag
        is reachable or git could not answer (a tag-less repo's first release
        runs through :mod:`hyperi_ci.version_source`).
    """
    tag = _last_version_tag()
    if tag is None:
        return None, []
    rc, commits = git_log([f"{tag}..HEAD"])
    if rc != 0:
        return None, []

    type_bump = load_type_bump(project_dir if project_dir is not None else Path.cwd())
    releasable = []
    for commit_hash, message in commits:
        bump = classify_commit(message, type_bump)
        if bump != "none":
            releasable.append((commit_hash, bump))
    return tag, releasable


def unreleased_warning(project_dir: Path | None = None) -> tuple[bool, str]:
    """Return ``(warn, message)`` for releasable work HEAD has not released.

    A validate-only run on main reports success either way, so this separates
    three answers: work waiting warns, nothing waiting stays quiet, and no
    baseline says so.

    Args:
        project_dir: Directory whose ``.releaserc.json`` overrides the bump
            map. Defaults to the current directory.

    Returns:
        ``(True, message)`` when HEAD carries releasable commits past its
        nearest ``v*`` tag; ``(False, message)`` otherwise, where the message
        names which of the two quiet answers it was.
    """
    tag, releasable = unreleased_since_tag(project_dir)
    if tag is None:
        return False, "no v* tag reachable from HEAD -- no released baseline to measure"
    if not releasable:
        return False, f"nothing releasable waiting since {tag}"

    counts = Counter(bump for _sha, bump in releasable)
    mix = ", ".join(
        f"{counts[bump]} {bump}" for bump in _BUMP_RENDER_ORDER if counts[bump]
    )
    verb = "commit sits" if len(releasable) == 1 else "commits sit"
    age = _tag_age_days(tag)
    tagged = "" if age is None else f", tagged {age} day{'' if age == 1 else 's'} ago"
    return True, (
        f"{len(releasable)} releasable {verb} unreleased since {tag}{tagged} "
        f"({mix}). This run validated them and published nothing. Ship them with "
        f"'hyperi-ci push --publish', or re-run this workflow with from-head=true."
    )
