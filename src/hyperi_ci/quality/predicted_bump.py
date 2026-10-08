# Project:   HyperI CI
# File:      src/hyperi_ci/quality/predicted_bump.py
# Purpose:   Predict the semver bump a publish would ship, for pre-push gating
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Predict the semver bump a publish would cut, from ``<last-tag>..HEAD``.

The commit-msg hook sees only the message being composed. A merge can bring
old ``feat!:`` or ``BREAKING CHANGE:`` commits into reach, which is how an
unintended major once shipped (issue #26). ``hyperi-ci push`` fails a predicted
minor or major unless ``HYPERCI_ALLOW_MINOR_BUMP=1`` or
``HYPERCI_ALLOW_MAJOR_BUMP=1`` is set.

The rules are :mod:`hyperi_ci.release_rules`, so no Node is needed. With no
prior tag, or no git, the prediction is ``none`` and the gate passes.
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from hyperi_ci.release_rules import _BUMP_ORDER, classify_commit, load_type_bump

# classify_commit is re-exported for existing importers.
__all__ = ["BumpPrediction", "classify_commit", "predict_bump"]


@dataclass
class BumpPrediction:
    """Outcome of analysing ``<last-tag>..HEAD``."""

    bump: str = "none"
    last_tag: str | None = None
    # Subjects of the commits behind a minor or major, for the gate message.
    minor_reasons: list[str] = field(default_factory=list)
    major_reasons: list[str] = field(default_factory=list)


def _git(args: list[str], cwd: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# Final-release tags only, as semantic-release honours; a `v2` or prerelease tag
# would otherwise win the sort and change the range.
_SEMVER_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _last_version_tag(cwd: str | None) -> str | None:
    result = _git(["tag", "--list", "v[0-9]*", "--sort=-v:refname"], cwd)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    for line in result.stdout.splitlines():
        tag = line.strip()
        if _SEMVER_TAG_RE.match(tag):
            return tag
    return None


def predict_bump(project_dir: Path | None = None) -> BumpPrediction:
    """Predict the bump ``<last-tag>..HEAD`` would ship.

    ``bump`` is ``"none"`` with no prior tag, no new commits, or no git.
    """
    cwd = str(project_dir) if project_dir else None
    prediction = BumpPrediction()

    last_tag = _last_version_tag(cwd)
    if last_tag is None:
        return prediction
    prediction.last_tag = last_tag

    # 0x1e ends each record and 0x1f separates hash from body, so multi-line
    # bodies parse intact.
    fmt = "%H%x1f%B%x1e"
    result = _git(["log", f"{last_tag}..HEAD", f"--format={fmt}"], cwd)
    if result.returncode != 0:
        return prediction

    type_bump = load_type_bump(project_dir or Path.cwd())
    best = "none"
    for record in result.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record or "\x1f" not in record:
            continue
        _sha, _, body = record.partition("\x1f")
        body = body.strip()
        if not body:
            continue
        bump = classify_commit(body, type_bump)
        subject = body.splitlines()[0]
        if bump == "minor":
            prediction.minor_reasons.append(subject)
        elif bump == "major":
            prediction.major_reasons.append(subject)
        if _BUMP_ORDER[bump] > _BUMP_ORDER[best]:
            best = bump
    prediction.bump = best
    return prediction
