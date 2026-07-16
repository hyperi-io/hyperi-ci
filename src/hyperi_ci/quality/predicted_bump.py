# Project:   HyperI CI
# File:      src/hyperi_ci/quality/predicted_bump.py
# Purpose:   Predict the semver bump a publish would ship, for pre-push gating
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Predict the semver bump a publish would cut, from the commit range.

The commit-msg hook checks what an agent *composes* (a single message +
``Publish: true`` trailer). It cannot see what a merge or cherry-pick
brings into *reachability*: a reconcile merge whose second parent carries
old ``feat!:`` / ``BREAKING CHANGE:`` commits imports a MAJOR bump the
agent never authored. That is exactly how hyperi-rustlib shipped an
unintended v3.0.0 to crates.io (2026-05-25, yanked). See issue #26.

This module analyses every commit in ``<last-tag>..HEAD`` - the same
range semantic-release's commit-analyzer walks - and returns the highest
bump those commits imply. ``hyperi-ci push`` gates on the result: a
predicted minor/major fails closed unless the operator sets
``HYPERCI_ALLOW_MINOR_BUMP=1`` / ``HYPERCI_ALLOW_MAJOR_BUMP=1``, the
reachability-level twin of ``HYPERCI_ALLOW_FEAT`` / ``HYPERCI_ALLOW_BREAKING``.

No Node / semantic-release install needed: the bump rules are
semantic-release's own default-release-rules, applied in Python via
:mod:`hyperi_ci.release_rules` (the SSoT). A repo's own ``.releaserc.json``
override is honoured there. When there is no prior tag (initial release) or
git is unavailable, the predictor returns ``none`` and the gate is a
no-op - fail open only when we genuinely cannot predict.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from hyperi_ci.release_rules import _BUMP_ORDER, classify_commit, load_type_bump

# Re-exported so existing call sites (and tests) keep importing classify_commit
# from this module. The bump SSoT itself lives in hyperi_ci.release_rules.
__all__ = ["BumpPrediction", "classify_commit", "predict_bump"]


@dataclass
class BumpPrediction:
    """Outcome of analysing ``<last-tag>..HEAD``."""

    bump: str = "none"
    last_tag: str | None = None
    # Subjects (first lines) of the commits that justified minor/major, so
    # the gate message can name the offenders.
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


# Strict final-release tags only (vX.Y.Z). A broad 'v*' glob lets a
# non-semver v-tag (vendor-x, v2, a prerelease) win the -v:refname sort
# and poison the analysis range — the gate would then walk a different
# range than semantic-release, which only honours semver tags. Matches
# the v[0-9]* + X.Y.Z discipline in the predict-version composite and
# push._compute_next_version.
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

    Returns a :class:`BumpPrediction`. ``bump == "none"`` when there is no
    prior tag, no new commits, or git is unavailable - the gate treats
    those as pass (nothing to over-bump, or nothing we can assert).
    """
    cwd = str(project_dir) if project_dir else None
    prediction = BumpPrediction()

    last_tag = _last_version_tag(cwd)
    if last_tag is None:
        # Initial release (no v* tag yet) - nothing to compare against.
        return prediction
    prediction.last_tag = last_tag

    # Null-delimit records and NUL-separate hash from body so multi-line
    # bodies survive parsing intact.
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
