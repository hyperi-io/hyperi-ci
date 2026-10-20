# Project:   HyperI CI
# File:      src/hyperi_ci/commit_range.py
# Purpose:   Resolve the commit range a CI event introduced, and its bump
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve the commits a CI event introduced, and whether they ship a release.

Shared by ``hyperi_ci.quality.commit_validation`` (validates every message in
the range) and the ``predict-version`` composite (is the range release-worthy,
did it change anything but docs, and what sits unreleased on a validate-only
run).

Stdlib-only, importing nothing from the package except
:mod:`hyperi_ci.release_rules` and, inside :func:`unreleased_since_tag`, the
stdlib-only :mod:`hyperi_ci.fork_version`, because the composite loads it BY
PATH where hyperi-ci is not installed. An import of ``common`` or ``config``
here breaks the plan job.
"""

import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

from hyperi_ci.release_rules import classify_commit, default_type_bump

_COMMIT_SEPARATOR = "----END----"
_COMMIT_FMT = f"%H%n%s%n%b%n{_COMMIT_SEPARATOR}"

# Highest bump first, for rendering the mix in a warning.
_BUMP_RENDER_ORDER = ("major", "minor", "patch")
_SECONDS_PER_DAY = 86400

#: Documentation, by suffix. ``quality.docs_touched`` classifies with it too.
DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".adoc"})
#: Source code, by suffix. Never harmless, whatever the file is named.
SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".rs",
        ".go",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".java",
        ".rb",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
    }
)
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg"})
_HARMLESS_NAMES = frozenset(
    {
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        "VERSION",
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "COPYING",
        "CHANGELOG",
        "CHANGELOG.md",
        "CHANGELOG.rst",
    }
)
# The British spelling, in any of its suffixed forms.
_HARMLESS_NAME_PREFIX = "LICENCE"

# How many paths a gate line names before it says "and N more".
_PATHS_NAMED = 5

#: Days unreleased work may wait before the plan job warns rather than notes.
UNRELEASED_WARN_DAYS_KEY = "release.unreleased_warn_days"


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


def git_log(
    args: list[str], cwd: Path | None = None
) -> tuple[int, list[tuple[str, str]]]:
    """Run ``git log --pretty=<fmt> <args>``; return ``(returncode, commits)``."""
    result = subprocess.run(
        ["git", "log", f"--pretty={_COMMIT_FMT}", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
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


def commits_in_range(
    *, first_parent: bool = False
) -> tuple[list[tuple[str, str]], bool]:
    """Return ``(commits, resolved)`` for the commits this CI event introduced.

    ``resolved`` is True when the range was determined authoritatively, even if
    empty. It is False when it could not be (shallow checkout, detached HEAD,
    missing ``before`` commit), and the caller MUST treat an empty result then
    as a degraded backstop, not success (issue #52).

    The range itself comes from :func:`resolve_range`.
    """
    _git_range, commits, resolved = resolve_range(first_parent=first_parent)
    return commits, resolved


def resolve_range(
    *, first_parent: bool = False
) -> tuple[str | None, list[tuple[str, str]], bool]:
    """Return ``(git_range, commits, resolved)`` for this CI event.

    ``git_range`` is the ``A..B`` spec the commits were read from, None when
    unresolved. ``commits`` and ``resolved`` are as :func:`commits_in_range`.
    ``first_parent`` reads only the first-parent chain, so a fork's
    upstream-sync merge contributes itself and not the upstream history behind it.

    Resolution, in order of authority:

    1. ``push`` -> ``before..after`` from the event payload. After a push to a
       tracked branch ``origin/<branch>`` already equals HEAD, so
       ``origin/main..HEAD`` would be empty and validate nothing. This range
       also covers merge-imported history.
    2. ``pull_request`` -> ``<base sha>..HEAD``.
    3. ``merge_group`` -> ``base_sha..head_sha`` from the payload, which is the
       squash commit the queue will fast-forward main to.
    4. Local / unknown contexts: ``origin/main..HEAD``, then ``HEAD~20..HEAD``.

    A resolved-but-empty range returns ``(range, [], True)`` without falling
    through.
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    payload = event_payload()
    walk = ["--first-parent"] if first_parent else []

    if event == "push":
        before = str(payload.get("before", ""))
        after = str(payload.get("after", "")) or "HEAD"
        # A real prior tip gives the authoritative range.
        if before and not is_zero_sha(before):
            git_range = f"{before}..{after}"
            rc, commits = git_log([*walk, git_range])
            if rc == 0:
                return git_range, commits, True
            # `before` is missing from a shallow clone. Falling through to
            # origin/main..HEAD would report an empty range (issue #52).
            return None, [], False
        # Branch creation (all-zeros before): fall through to the generic ranges.
    elif event == "pull_request":
        base = str((payload.get("pull_request") or {}).get("base", {}).get("sha", ""))
        if not base and os.environ.get("GITHUB_BASE_REF"):
            base = f"origin/{os.environ['GITHUB_BASE_REF']}"
        if base:
            git_range = f"{base}..HEAD"
            rc, commits = git_log([*walk, git_range])
            if rc == 0:
                return git_range, commits, True
    elif event == "merge_group":
        group = payload.get("merge_group") or {}
        base = str(group.get("base_sha", ""))
        head = str(group.get("head_sha", "")) or "HEAD"
        if base:
            git_range = f"{base}..{head}"
            rc, commits = git_log([*walk, git_range])
            if rc == 0:
                return git_range, commits, True
            # As for push: a fallback range would validate the wrong set.
            return None, [], False

    for git_range in ("origin/main..HEAD", "HEAD~20..HEAD"):
        rc, commits = git_log([*walk, git_range])
        if rc == 0:
            return git_range, commits, True

    return None, [], False


def is_release_worthy(project_dir: Path | None = None) -> tuple[bool, str]:
    """Return ``(worthy, reason)`` for the commits this CI event introduced.

    Worthy means at least one commit resolves to a bump other than ``none``
    under :mod:`hyperi_ci.release_rules`.

    An UNRESOLVABLE range returns True so a shallow clone cannot silently skip
    the quality and test gate (no silent skips). A resolved-but-empty range
    returns False.
    """
    # Imported here because fork_version imports this module.
    from hyperi_ci.fork_version import check_fork

    root = project_dir if project_dir is not None else Path.cwd()
    commits, resolved = commits_in_range(first_parent=check_fork(root).fork)
    if not resolved:
        return True, "could not resolve the pushed range -- running the checks"
    if not commits:
        return False, "no commits in the pushed range"

    type_bump = default_type_bump()
    for commit_hash, message in commits:
        bump = classify_commit(message, type_bump)
        if bump != "none":
            subject = message.splitlines()[0] if message else ""
            return True, f"{commit_hash[:8]} is a {bump} bump: {subject}"

    return False, f"no release-worthy commit in {len(commits)} pushed commit(s)"


def is_harmless_path(path: str) -> bool:
    """Return True for a file no quality or test job can be affected by.

    Docs, images and repo metadata, by exact name. Anything else, a workflow or
    a config file included, counts as code, and so does any source suffix,
    under ``docs/`` or named ``LICENCE.py`` alike.
    """
    posix = path.strip().replace("\\", "/")
    name = posix.rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    if suffix in SOURCE_SUFFIXES:
        return False
    return (
        posix.startswith("docs/")
        or suffix in DOC_SUFFIXES
        or suffix in _IMAGE_SUFFIXES
        or name in _HARMLESS_NAMES
        or name.startswith(_HARMLESS_NAME_PREFIX)
    )


def changed_paths(git_range: str, cwd: Path | None = None) -> list[str] | None:
    """Return every path ``git_range`` changed, or None when git cannot say.

    Renames are listed as a delete plus an add, so moving code into ``docs/``
    still names the code path it left.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", git_range],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def _name_some(paths: list[str]) -> str:
    shown = ", ".join(paths[:_PATHS_NAMED])
    extra = len(paths) - _PATHS_NAMED
    return f"{shown} and {extra} more" if extra > 0 else shown


def touches_code() -> tuple[bool, str]:
    """Return ``(touched, reason)``: did this CI event change anything but docs?

    A ``chore:`` or ``test:`` merge that edits code is not release-worthy, and
    still has to be checked (issue #96). False means every changed path passed
    :func:`is_harmless_path`, and the reason names them.

    An unresolvable range, a push that created the branch, a push that added
    no commits (a rewind), or a diff git cannot produce, returns True: the
    answer decides whether quality and test run, so doubt runs them.
    """
    if os.environ.get("GITHUB_EVENT_NAME") == "push" and is_zero_sha(
        str(event_payload().get("before", ""))
    ):
        return True, "the push created the branch, so it has no prior tip to diff"
    git_range, commits, resolved = resolve_range()
    if not resolved or git_range is None:
        return True, "could not resolve the pushed range -- running the checks"
    if not commits:
        return True, "the pushed range adds no commits, a rewind or a re-run"

    paths = changed_paths(git_range)
    if paths is None:
        return (
            True,
            f"could not list the files {git_range} changed -- running the checks",
        )

    code = [path for path in paths if not is_harmless_path(path)]
    if code:
        return True, f"{len(code)} changed file(s) outside docs: {_name_some(code)}"
    if not paths:
        return False, "the pushed range changed no files"
    return False, f"only docs and repo metadata changed: {_name_some(paths)}"


def last_version_tag(
    *, first_parent: bool = False, cwd: Path | None = None, rev: str = "HEAD"
) -> str | None:
    """Return the nearest ``v*`` tag reachable from ``rev``, or None if there is none.

    Args:
        first_parent: Walk only HEAD's first-parent chain and skip prerelease
            tags, so a tag reachable only through a merged-in branch is never
            taken as this branch's last release.
        cwd: Repository to ask. Defaults to the current directory.
        rev: The commit to look back from. Defaults to HEAD.

    Returns:
        The tag name, or None when no tag matches or git could not answer.

    """
    args = ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"]
    if first_parent:
        args += ["--exclude", "v*-*", "--first-parent"]
    args.append(rev)
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
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

    Cumulative, not per-push: every releasable commit the last tag lacks. A
    fork counts only its own first-parent commits past its own last tag.

    Args:
        project_dir: Repository to read. Defaults to the current directory.

    Returns:
        ``(tag, commits)`` where ``commits`` is ``(sha, bump)`` for each
        releasable commit in ``tag..HEAD``. ``(None, [])`` when no ``v*`` tag
        is reachable or git could not answer (a tag-less repo's first release
        runs through :mod:`hyperi_ci.version_source`).
    """
    # Imported here because fork_version imports this module.
    from hyperi_ci.fork_version import ForkVersionError, check_fork, fork_commit_bumps

    root = project_dir if project_dir is not None else Path.cwd()
    if check_fork(root).fork:
        tag = last_version_tag(first_parent=True, cwd=root)
        if tag is None:
            return None, []
        try:
            bumps = fork_commit_bumps(root, tag)
        except ForkVersionError:
            return None, []
        return tag, [(sha, bump) for sha, _subject, bump in bumps if bump != "none"]

    tag = last_version_tag(cwd=project_dir)
    if tag is None:
        return None, []
    rc, commits = git_log([f"{tag}..HEAD"], cwd=project_dir)
    if rc != 0:
        return None, []

    type_bump = default_type_bump()
    releasable = []
    for commit_hash, message in commits:
        bump = classify_commit(message, type_bump)
        if bump != "none":
            releasable.append((commit_hash, bump))
    return tag, releasable


def oldest_commit_age_days(shas: list[str], cwd: Path | None = None) -> float | None:
    """Return how many days ago the oldest of ``shas`` was committed.

    Committer time, which for a squash merge is when it landed on the branch.
    None when ``shas`` is empty or git could not say.
    """
    if not shas:
        return None
    result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", *shas],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    try:
        oldest = min(int(line) for line in result.stdout.split())
    except ValueError:
        return None
    return max(0.0, (time.time() - oldest) / _SECONDS_PER_DAY)


def unreleased_annotation(
    project_dir: Path | None = None, *, warn_days: float | None
) -> tuple[str, str]:
    """Return ``(level, message)`` for releasable work HEAD has not released.

    A validate-only run on main reports success either way, so this separates
    three answers: work waiting is reported, nothing waiting stays quiet, and
    no baseline says so. Work waiting is a ``warning`` once its oldest commit
    is more than ``warn_days`` old, else a ``notice`` with the same message.

    Args:
        project_dir: Repository to read. Defaults to the current directory.
        warn_days: ``release.unreleased_warn_days``. None, or an age git
            cannot read, warns.

    Returns:
        ``("warning" | "notice", message)`` when HEAD carries releasable
        commits past its nearest ``v*`` tag; ``("", message)`` otherwise,
        where the message names which of the two quiet answers it was.
    """
    tag, releasable = unreleased_since_tag(project_dir)
    if tag is None:
        return "", "no v* tag reachable from HEAD -- no released baseline to measure"
    if not releasable:
        return "", f"nothing releasable waiting since {tag}"

    age = oldest_commit_age_days([sha for sha, _bump in releasable], project_dir)
    young = warn_days is not None and age is not None and age <= warn_days
    return ("notice" if young else "warning"), _unreleased_message(tag, releasable)


def _unreleased_message(tag: str, releasable: list[tuple[str, str]]) -> str:
    # A consumer keys findings on this text, so it changes only on purpose.
    counts = Counter(bump for _sha, bump in releasable)
    mix = ", ".join(
        f"{counts[bump]} {bump}" for bump in _BUMP_RENDER_ORDER if counts[bump]
    )
    verb = "commit sits" if len(releasable) == 1 else "commits sit"
    age = _tag_age_days(tag)
    tagged = "" if age is None else f", tagged {age} day{'' if age == 1 else 's'} ago"
    return (
        f"{len(releasable)} releasable {verb} unreleased since {tag}{tagged} "
        f"({mix}). This run validated them and published nothing. Ship them with "
        f"'hyperi-ci push --publish', or re-run this workflow with from-head=true."
    )
