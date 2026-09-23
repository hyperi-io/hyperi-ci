# Project:   HyperI CI
# File:      src/hyperi_ci/commit_range.py
# Purpose:   Resolve the commit range a CI event introduced, and its bump
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve the commits a CI event introduced, and whether they ship a release.

Three callers share this, which is why it lives on its own:

- ``hyperi_ci.quality.commit_validation`` validates every message in the range.
- The ``predict-version`` composite asks whether the range is release-worthy,
  to decide whether quality + test run on a push to main.
- The same composite asks what is sitting UNRELEASED on a validate-only run,
  which is the cumulative question rather than the per-push one.

Stdlib-only, and it imports nothing from the package except
:mod:`hyperi_ci.release_rules` (itself stdlib-only), because the composite
loads it BY PATH in a job where hyperi-ci is not installed -- the same
constraint ``version_source.py`` carries. Adding an import of ``common`` or
``config`` here breaks the plan job.
"""

from __future__ import annotations

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


def _event_payload() -> dict:
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

    ``resolved`` is True when we authoritatively determined the range the
    event introduced (even if it is empty -- a legitimate "no new commits").
    It is False when we could NOT resolve the range (shallow checkout,
    detached HEAD, missing ``before`` commit) -- the caller MUST then treat
    an empty result as a DEGRADED backstop, not as success (issue #52).

    Resolution, in order of authority:

    1. ``push`` event -> ``before..after`` from the event payload. This is
       the ONLY correct range on a push to a tracked branch: after the push,
       the runner's ``origin/<branch>`` already points at HEAD, so
       ``origin/main..HEAD`` is empty and would silently validate nothing.
       Also catches merge-imported history (the range includes commits a
       merge made newly reachable) -- the rustlib v3.0.0 class of bug.
    2. ``pull_request`` event -> ``<base sha>..HEAD``.
    3. Generic fallbacks for local / unknown contexts: ``origin/main..HEAD``
       then a bounded ``HEAD~N..HEAD``.

    A resolved-but-empty range short-circuits (returns ``([], True)``) so we
    don't fall through and mis-resolve against a different range.
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    payload = _event_payload()

    if event == "push":
        before = str(payload.get("before", ""))
        after = str(payload.get("after", "")) or "HEAD"
        # A real prior tip gives the authoritative range.
        if before and not is_zero_sha(before):
            rc, commits = git_log([f"{before}..{after}"])
            if rc == 0:
                return commits, True
            # We KNOW new commits exist (before != after) but can't enumerate
            # them - `before` isn't in this shallow clone. Do NOT fall through
            # to origin/main..HEAD: right after a push-to-main that range is
            # EMPTY (origin/main already == HEAD) and would wrongly report "no
            # new commits" - the exact silent no-op of issue #52. Degrade to
            # the HEAD-only backstop with a loud warning instead.
            return [], False
        # before is all-zeros (branch creation): no prior tip to diff from, so
        # fall through to the generic ranges (origin/main..HEAD enumerates what
        # the new branch adds over main).
    elif event == "pull_request":
        base = str((payload.get("pull_request") or {}).get("base", {}).get("sha", ""))
        if not base and os.environ.get("GITHUB_BASE_REF"):
            base = f"origin/{os.environ['GITHUB_BASE_REF']}"
        if base:
            rc, commits = git_log([f"{base}..HEAD"])
            if rc == 0:
                return commits, True

    for git_range in ("origin/main..HEAD", "HEAD~20..HEAD"):
        rc, commits = git_log([git_range])
        if rc == 0:
            return commits, True

    return [], False


def is_release_worthy(project_dir: Path | None = None) -> tuple[bool, str]:
    """Return ``(worthy, reason)`` for the commits this CI event introduced.

    Worthy means at least one commit in the range resolves to a bump other
    than ``none`` under :mod:`hyperi_ci.release_rules` -- semantic-release's
    own defaults, overridden only by a repo ``.releaserc.json``.

    An UNRESOLVABLE range returns True: the gate this feeds decides whether
    quality and test run, and a gate that silently skips itself because the
    clone was shallow is the failure this exists to prevent (design principle
    3, no silent skips). A resolved-but-empty range is a real answer, not a
    degradation, so it returns False.
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

    Cumulative, not per-push: :func:`is_release_worthy` answers whether ONE
    push shipped something, which goes quiet again on the next chore commit.
    What an engineer can act on is the backlog -- every releasable commit the
    last tag does not include.

    Args:
        project_dir: Directory whose ``.releaserc.json`` overrides the bump
            map. Defaults to the current directory.

    Returns:
        ``(tag, commits)`` where ``commits`` is ``(sha, bump)`` for each
        releasable commit in ``tag..HEAD``. ``(None, [])`` when no ``v*`` tag
        is reachable or git could not answer -- a tag-less repo has no
        released baseline to measure against, and its first release runs
        through :mod:`hyperi_ci.version_source` instead.
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

    A validate-only run on main is correct by design and reports success, so
    "nothing to ship" and "thirteen fixes waiting" arrive looking identical.
    Three answers, never two: work waiting warns, nothing waiting stays quiet,
    and no measurable baseline says so rather than passing for either.

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
