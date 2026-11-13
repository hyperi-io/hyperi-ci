# Project:   HyperI CI
# File:      src/hyperi_ci/release_candidate.py
# Purpose:   Turn a predicted stable version into the next release candidate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release candidates: every release from main ships as ``X.Y.Z-rc.N``.

A repo opts in with ``release.candidates: true``. semantic-release still
predicts ``X.Y.Z``, because main is its only release branch and it never counts
a prerelease tag as the last release, so the suffix is added here. ``N`` is one
more than the highest ``vX.Y.Z-rc.N`` tag already cut. ``hyperi-ci promote``
later tags the chosen candidate's commit ``vX.Y.Z``.

A ``Hotfix:`` trailer on the release commit ships ``X.Y.Z`` directly. The
trailer carries the reason, which the release reports.

Stdlib-only apart from :mod:`hyperi_ci.project_config`, because the
predict-version composite loads it by path where hyperi-ci is not installed.
"""

# predict-version loads this file by path on the runner's python3, which may predate 3.14.
from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from hyperi_ci.project_config import find_config, read_project_config, setting

CANDIDATES_KEY = "release.candidates"
HOTFIX_TRAILER = "Hotfix"

_STABLE = re.compile(r"^\d+\.\d+\.\d+$")
_CANDIDATE = re.compile(r"^\d+\.\d+\.\d+-rc\.[1-9]\d*$")
# YAML spells true several ways and a repo may quote it.
_ON = frozenset({"true", "yes", "on", "1"})


class Candidate(NamedTuple):
    """The version a release ships as.

    Attributes:
        version: The version to tag, with or without the ``-rc.N`` suffix.
        candidate: True when ``version`` is a release candidate.
        reason: One line saying why.
        warning: True when the reason is worth a warning: a hotfix, or a
            config read as off because it could not be parsed.

    """

    version: str
    candidate: bool
    reason: str
    warning: bool = False


def wants_candidates(root: Path) -> tuple[bool, str, bool]:
    """Return whether the project ships release candidates.

    A config that cannot be parsed reads as off when its text never mentions
    the key, as on a runner with no YAML parser, and that answer warns.

    Returns:
        The answer, where it came from, and whether to warn about it.

    Raises:
        ValueError: The config cannot be parsed and mentions the key, so
            either answer could ship the wrong kind of release.

    """
    project = read_project_config(root)
    if project.unreadable:
        path = find_config(root)
        text = path.read_text(encoding="utf-8", errors="replace") if path else ""
        if CANDIDATES_KEY.rsplit(".", 1)[-1] in text:
            raise ValueError(project.unreadable)
        return False, f"{project.unreadable}, and it names no candidates", True
    value, source = setting(root, CANDIDATES_KEY)
    if value is None:
        return False, source, False
    return str(value).strip().lower() in _ON, source, False


def candidate_version(value: str | None) -> str | None:
    """Return bare ``X.Y.Z-rc.N`` if ``value`` names a candidate, else None."""
    bare = value.strip().removeprefix("v") if value else ""
    return bare if _CANDIDATE.match(bare) else None


def next_candidate(version: str, tags: Iterable[str]) -> str:
    """Return ``version-rc.N``, one past the highest candidate tag for it."""
    pattern = re.compile(rf"^v{re.escape(version)}-rc\.(\d+)$")
    cut = [int(m.group(1)) for tag in tags if (m := pattern.match(tag.strip()))]
    return f"{version}-rc.{max(cut, default=0) + 1}"


def resolve(root: Path, version: str, tags: Iterable[str], hotfix: str) -> Candidate:
    """Decide what a release of ``version`` ships as.

    Args:
        root: The checkout root holding the project config.
        version: The version the plan predicted.
        tags: Every tag in the repo.
        hotfix: The release commit's ``Hotfix:`` trailer, empty when absent.

    Returns:
        The version to release, whether it is a candidate, and the reason.

    """
    wanted, source, warning = wants_candidates(root)
    if not wanted:
        return Candidate(version, False, f"{CANDIDATES_KEY} is off ({source})", warning)
    if not _STABLE.match(version):
        return Candidate(version, False, f"v{version} is already a prerelease")
    if hotfix:
        return Candidate(version, False, f"hotfix, no candidate: {hotfix}", True)
    return Candidate(next_candidate(version, tags), True, f"{CANDIDATES_KEY} is on")


def repo_tags(root: Path) -> list[str]:
    """Return every tag in the checkout, empty when git fails."""
    return _git(["tag", "--list"], root).splitlines()


def hotfix_reason(root: Path) -> str:
    """Return HEAD's ``Hotfix:`` trailers on one line, empty when it has none."""
    trailer = f"--format=%(trailers:key={HOTFIX_TRAILER},valueonly)"
    # One line, because the reason lands in a workflow command.
    return " ".join(_git(["log", "-1", trailer], root).split())


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
