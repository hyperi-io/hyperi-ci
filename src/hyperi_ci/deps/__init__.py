# Project:   HyperI CI
# File:      src/hyperi_ci/deps/__init__.py
# Purpose:   `hyperi-ci deps` -- enumerate dependency surfaces, audit floors
# Origin:    Derek's deps automation scripts, merged into hyperi-ci now they
#            are mature enough for people to use directly -- and for hyperi-ai's
#            /deps skill to drive.
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Dependency-surface enumeration, floor/lock drift, Renovate blind spots.

Grown out of Derek's deps automation scripts and merged here once they were
mature enough to be worth handing to other people -- both to run at a terminal
and for hyperi-ai's ``/deps`` skill to call.

The PREVENTATIVE half of the dependency chain. It runs LOCALLY, before a change
reaches CI or the forge, and says what you are about to leave stale. Renovate
is the remediation half: it runs after the fact and raises PRs for what already
went stale. Policy and cooldowns live there and in
``scripts/update-versions.py``; nothing in here makes a judgement call.

Three operations, and one that runs all of them:

- ``scan``   -- match every tracked file against ``config/dep-surfaces.yaml``
  and extract the versions embedded in each. Three states per surface, never
  two: ``found``, ``inert`` (files matched, nothing extractable), ``absent``.
- ``drift``  -- declared floor vs locked version, per dependency GROUP.
  Renovate has no equivalent, so this standing audit is the original half.
- ``gaps``   -- which present surfaces the repo's Renovate config never sees.
- ``report`` -- all three in one pass, which is what bare ``hyperi-ci deps``
  prints. Every extra round trip is a chance for a caller to give up and
  hand-roll a grep, so the first answer is the whole picture.

``show(root, surface_id)`` pre-cans the obvious follow-up: every matched file,
every pin with its line number, and the declared-vs-locked table for that one
surface, uncapped.
"""

from __future__ import annotations

from pathlib import Path

from hyperi_ci.deps.ecosystems import drift
from hyperi_ci.deps.renovate import gaps
from hyperi_ci.deps.surfaces import Surface, load, repo_files, scan

__all__ = ["Surface", "drift", "gaps", "load", "report", "scan", "show"]


def report(
    root: Path, surfaces: tuple[Surface, ...] | None = None, kind: str = ""
) -> dict:
    """Everything in one pass: surfaces, pins, groups, drift, Renovate gaps.

    The entry point Derek's original scripts grew towards and never had: one
    call that answers the whole question rather than a run of separate probes.

    Args:
        root: Repository root.
        surfaces: Override catalogue (tests).
        kind: Optional surface ``kind`` filter (python, rust, container, ...).
            Filters the surface list AND the matching ecosystems, for when a
            polyglot repo's full report is more than you want at once.

    Returns:
        ``{root, kind_filter, scan, drift, gaps}``.

    """
    root = Path(root).resolve()
    catalogue = surfaces if surfaces is not None else load()
    files, source = repo_files(root)
    scan_result = scan(root, catalogue, files=files)
    # scan() cannot know where a caller-supplied list came from, so it labels
    # it "caller". Put the real answer back: a reader needs to know whether
    # gitignored files were excluded or a raw walk was used.
    scan_result["file_source"] = source
    drift_result = drift(root, catalogue, files=files)
    gaps_result = gaps(root, scan_result)

    if kind:
        scan_result = dict(scan_result)
        scan_result["surfaces"] = [
            r for r in scan_result["surfaces"] if r["kind"] == kind
        ]
        keep = {r["id"] for r in scan_result["surfaces"]}
        gaps_result = dict(gaps_result)
        gaps_result["uncovered"] = [
            u for u in gaps_result["uncovered"] if u["id"] in keep
        ]
        drift_result = dict(drift_result)
        drift_result["ecosystems"] = [
            e for e in drift_result["ecosystems"] if e["name"] == kind
        ]
        drift_result["drift"] = [
            d for d in drift_result["drift"] if d["ecosystem"] == kind
        ]

    return {
        "root": str(root),
        "kind_filter": kind,
        "scan": scan_result,
        "drift": drift_result,
        "gaps": gaps_result,
    }


def show(
    root: Path, surface_id: str, surfaces: tuple[Surface, ...] | None = None
) -> dict:
    """Full detail for ONE surface -- the pre-canned follow-up.

    Every matched file, every pin with its line number and current value, the
    catalogue entry behind it (patterns, notes, gap, caveat), and the
    declared-vs-locked table where the surface owns a manifest. Nothing capped:
    this is the answer to "show me everything about X", which is otherwise
    where somebody starts writing ad-hoc rg pipelines.

    Returns:
        The detail dict, or ``{"error": ..., "known": [...]}`` for a bad id.

    """
    root = Path(root).resolve()
    catalogue = surfaces if surfaces is not None else load()
    match = next((s for s in catalogue if s.id == surface_id), None)
    if match is None:
        return {
            "error": f"unknown surface id {surface_id!r}",
            "known": [s.id for s in catalogue],
        }

    files, _ = repo_files(root)
    scan_result = scan(root, catalogue, files=files)
    record = next(r for r in scan_result["surfaces"] if r["id"] == surface_id)

    ecosystems: list[dict] = []
    if match.groups:
        owned = set(record["files"])
        ecosystems = [
            eco
            for eco in drift(root, catalogue, files=files)["ecosystems"]
            if eco["manifest"] in owned
        ]

    gaps_result = gaps(root, scan_result)
    return {
        "root": str(root),
        "surface": record,
        "registry": {
            "patterns": list(match.raw_patterns),
            "groups": list(match.groups),
            "lock": list(match.lock),
            "renovate_manager": match.renovate_manager,
            "gap": match.gap,
            "caveat": match.caveat,
            "notes": match.notes,
        },
        "ecosystems": ecosystems,
        "renovate": next(
            (u for u in gaps_result["uncovered"] if u["id"] == surface_id), None
        ),
    }
