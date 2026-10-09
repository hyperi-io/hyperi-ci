# Project:   HyperI CI
# File:      src/hyperi_ci/fork_version.py
# Purpose:   Version a fork's release from its own first-parent commits
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Version a fork's release from its own first-parent commits.

semantic-release analyses every commit reachable from HEAD, so a fork that
merges its upstream counts upstream's ``feat:`` and breaking commits as its
own.

A repo classified ``fork`` releasing a stable version from main walks HEAD's
first-parent chain instead. Each sync merge is one commit there, classified by
its own message under :mod:`hyperi_ci.release_rules`. A prerelease branch keeps
semantic-release.

Stdlib-only, importing nothing from the package but ``classification``,
``commit_range``, ``project_config``, ``release_rules`` and ``version_source``,
because the predict-version composite loads it by path where hyperi-ci is not
installed.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

from hyperi_ci.classification import resolve
from hyperi_ci.commit_range import git_log, last_version_tag
from hyperi_ci.project_config import read_project_config
from hyperi_ci.release_rules import _BUMP_ORDER, classify_commit, load_type_bump
from hyperi_ci.version_source import seed_version

#: The canonical classification that versions from first-parent commits.
FORK = "fork"

# `hyperi-ci config` lets this variable replace the file's `classification`.
_ENV_OVERRIDE = "HYPERCI_CLASSIFICATION"

_STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

_RELEASE_GUIDANCE = (
    "On a push, drop the 'Release: true' trailer (this is a non-release commit) "
    "or land a fix:/feat:/perf: commit. On a from-head dispatch, re-run with "
    "--bump patch (or minor) to force a release."
)


class ForkVersionError(Exception):
    """The first-parent history gives no version this run can release."""


class ForkCheck(NamedTuple):
    """Whether a repo versions from first-parent commits, and why.

    Attributes:
        fork: True when the repo is classified ``fork``.
        reason: One line naming the marker that answered.
        warning: Why the answer may be wrong, else empty.

    """

    fork: bool
    reason: str
    warning: str


def check_fork(root: Path) -> ForkCheck:
    """Read the repo's classification the way ``hyperi-ci config`` does.

    The release tail asks the published CLI the same question, so a marker
    the CLI reads as undeclared must read as undeclared here too.

    Args:
        root: The checkout root.

    Returns:
        The answer. An unparseable config leaves only the dotfile to answer,
        and says so in ``warning``.

    """
    project = read_project_config(root)
    merged: dict[str, Any] = dict(project.data or {})
    if _ENV_OVERRIDE in os.environ:
        merged["classification"] = os.environ[_ENV_OVERRIDE]
    unreadable = project.unreadable if _ENV_OVERRIDE not in os.environ else ""

    try:
        resolution = resolve(merged, root)
    except (ValueError, OSError) as exc:
        return ForkCheck(
            False, "classification unreadable", f"{exc} Read as undeclared."
        )

    fork = resolution.value == FORK
    declared = resolution.value or "undeclared"
    reason = f"classification {declared} ({resolution.source})"
    warning = ""
    if unreadable and not fork:
        warning = f"{unreadable}, so a fork declared there is not seen"
    return ForkCheck(fork, reason, warning)


def next_version(base: str, bump: str) -> str:
    """Apply a bump level to a plain ``X.Y.Z``, as semantic-release does.

    Args:
        base: The last released version.
        bump: ``patch``, ``minor`` or ``major``.

    Returns:
        The bumped version.

    Raises:
        ValueError: ``base`` is not ``X.Y.Z`` or ``bump`` is not a level.

    """
    major, minor, patch = (int(part) for part in base.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"not a bump level: {bump!r}")


def predict_version(root: Path) -> tuple[str, str]:
    """Return the version a fork's release from main ships, and how.

    Args:
        root: The checkout root, run on main's HEAD with tags fetched.

    Returns:
        ``(version, explanation)``.

    Raises:
        ForkVersionError: No release-worthy first-parent commit, a ``v*`` tag
            that is not on the first-parent chain, or a predicted tag that
            already names another commit.

    """
    type_bump = load_type_bump(root)
    tag = last_version_tag(first_parent=True, cwd=root)
    if tag is None:
        return _first_release(root, type_bump)

    match = _STABLE_TAG.match(tag)
    if match is None:
        raise ForkVersionError(f"Last first-parent tag {tag} is not vX.Y.Z.")
    base = ".".join(match.groups())

    commits = _first_parent_commits(f"{tag}..HEAD", root)
    bump, decider = _highest_bump(commits, type_bump)
    if bump == "none":
        raise ForkVersionError(
            f"No release-worthy first-parent commits since {tag} "
            f"({len(commits)} checked). {_RELEASE_GUIDANCE}"
        )

    version = next_version(base, bump)
    _refuse_taken_tag(version, root)
    return version, (
        f"{bump} from {tag} over {len(commits)} first-parent commit(s), "
        f"decided by {decider}"
    )


def _first_release(root: Path, type_bump: dict[str, str]) -> tuple[str, str]:
    """Version a fork with no tag on its first-parent chain."""
    if _git(["tag", "--list", "v[0-9]*"], root):
        raise ForkVersionError(
            "v* tags exist but none is on HEAD's first-parent history: either "
            "orphaned by a past history rewrite (issue #37) or brought in only "
            "by a merge from upstream. Mark the fork's own baseline with "
            "'hyperi-ci publish --version X.Y.Z', or recover orphaned tags with "
            "scripts/recover-tags.py."
        )
    commits = _first_parent_commits("HEAD", root)
    bump, _ = _highest_bump(commits, type_bump)
    if bump == "none":
        raise ForkVersionError(
            f"No release-worthy first-parent commits on a tag-less repo "
            f"({len(commits)} checked). {_RELEASE_GUIDANCE}"
        )
    version, source = seed_version(root)
    return (
        version,
        f"first release on a tag-less repo, starting at {version} ({source})",
    )


class _Commit(NamedTuple):
    sha: str
    message: str
    merge: bool


def _first_parent_commits(revisions: str, root: Path) -> list[_Commit]:
    """Return the first-parent commits in ``revisions``, each marked merge or not.

    Raises:
        ForkVersionError: git could not list them.

    """
    rc, commits = git_log(["--first-parent", revisions], cwd=root)
    if rc != 0:
        raise ForkVersionError(f"git log --first-parent {revisions} exited {rc}.")
    merges = set(
        _git(["rev-list", "--first-parent", "--merges", revisions], root).split()
    )
    return [_Commit(sha, message, sha in merges) for sha, message in commits]


def _commit_bump(commit: _Commit, type_bump: dict[str, str]) -> str:
    """Return the bump one first-parent commit implies.

    A merge is read by its subject alone, because its body can quote upstream's
    commits, and one whose subject is not a conventional commit, such as a sync
    merge, ships as a patch.
    """
    if not commit.merge:
        return classify_commit(commit.message, type_bump)
    subject = commit.message.splitlines()[0] if commit.message else ""
    bump = classify_commit(subject, type_bump)
    return "patch" if bump == "none" else bump


def _highest_bump(commits: list[_Commit], type_bump: dict[str, str]) -> tuple[str, str]:
    """Return the highest bump in ``commits`` and the commit that set it."""
    best = "none"
    decider = ""
    for commit in commits:
        bump = _commit_bump(commit, type_bump)
        if _BUMP_ORDER[bump] > _BUMP_ORDER[best]:
            best = bump
            subject = commit.message.splitlines()[0] if commit.message else ""
            decider = f"{commit.sha[:8]} {subject}"
    return best, decider


def _refuse_taken_tag(version: str, root: Path) -> None:
    """Raise when ``v<version>`` already names a commit other than HEAD."""
    existing = _git(
        ["rev-parse", "-q", "--verify", f"refs/tags/v{version}^{{commit}}"], root
    )
    if not existing:
        return
    head = _git(["rev-parse", "HEAD^{commit}"], root)
    if existing != head:
        raise ForkVersionError(
            f"Predicted v{version} already exists at {existing} (HEAD is {head}) "
            "-- refusing to re-release it (issue #37). Ship past it with "
            "'hyperi-ci publish --version X.Y.Z' (or --bump patch)."
        )


def _git(args: list[str], root: Path) -> str:
    """Run git in ``root`` and return its stripped stdout, empty on failure."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=root,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""
