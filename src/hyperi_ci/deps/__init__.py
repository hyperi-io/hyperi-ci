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

The local, preventative half of the dependency chain: it reports what a change
is about to leave stale, while Renovate raises PRs after the fact. Policy and
cooldowns live in Renovate and ``scripts/update-versions.py``, not here.

- ``scan``   -- match tracked files against ``config/dep-surfaces.yaml`` and
  extract embedded versions; each surface is ``found``, ``inert`` or ``absent``.
- ``drift``  -- declared floor vs locked version, per dependency group.
- ``gaps``   -- present surfaces the repo's Renovate config never sees.
- ``report`` -- all three in one pass, as bare ``hyperi-ci deps`` prints.
- ``show``   -- every file, pin and group for one surface, uncapped.
"""

from pathlib import Path

from hyperi_ci.deps.ecosystems import drift
from hyperi_ci.deps.renovate import gaps
from hyperi_ci.deps.surfaces import Surface, load, repo_files, scan

__all__ = ["Surface", "drift", "gaps", "load", "report", "scan", "show"]


def report(
    root: Path, surfaces: tuple[Surface, ...] | None = None, kind: str = ""
) -> dict:
    """Run scan, drift and gaps in one pass.

    Args:
        root: Repository root.
        surfaces: Override catalogue (tests).
        kind: Optional surface ``kind`` filter (python, rust, container, ...),
            applied to the surfaces, the gaps and the drift ecosystems.

    Returns:
        ``{root, kind_filter, scan, drift, gaps}``.

    """
    root = Path(root).resolve()
    catalogue = surfaces if surfaces is not None else load()
    files, source = repo_files(root)
    scan_result = scan(root, catalogue, files=files)
    # scan() labels a passed-in list "caller"; the reader needs git vs walk.
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
    """Return full, uncapped detail for one surface.

    Covers every matched file and pin, the catalogue entry, the
    declared-vs-locked groups where the surface owns a manifest, and its
    Renovate gap.

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
