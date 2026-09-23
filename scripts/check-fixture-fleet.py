#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/check-fixture-fleet.py
# Purpose:   Gate - config/fixtures.yaml matches the org's real ci-test-* set
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Fixture-fleet consistency gate (issue #215).

The fleet used to live in CLAUDE.md prose, which is untracked here, so the
only record of which fixtures exist was a file nobody else could read - and
it said 8 while the org had 9. A list in prose cannot be checked against
anything.

This compares `config/fixtures.yaml` against the repos the org actually has.
A fixture renamed, added or deleted surfaces the same day instead of the next
time somebody notices a sweep ran over four repos that no longer exist.

Needs network and an authenticated `gh`, so it belongs on the scheduled audit
rather than the PR path.

Usage:  uv run scripts/check-fixture-fleet.py
Exit 1 on drift, 2 when the org cannot be reached, 0 otherwise.
"""

import json
import subprocess
import sys
from pathlib import Path

# The sibling module is a plain file, not a package, so the directory has to be
# on the path before the import -- importlib-loaded callers get no sys.path[0].
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_fleet  # noqa: E402

_ORG = fixture_fleet.ORG
_PREFIX = "ci-test-"


def declared() -> set[str]:
    """Fixture names this repo claims exist."""
    return fixture_fleet.declared_names(fixture_fleet.load_fleet())


def actual() -> set[str] | None:
    """Fixture names the org has, or None when it cannot be reached.

    None is its own outcome, not an empty set. An unreachable org and an org
    with no fixtures are the same value otherwise, and reporting "every
    fixture was deleted" on a network blip is worse than saying nothing.
    """
    result = subprocess.run(
        ["gh", "repo", "list", _ORG, "--limit", "200", "--json", "name"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        repos = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return {r["name"] for r in repos if r["name"].startswith(_PREFIX)}


def main() -> int:
    """Compare the declared fleet against the org. Returns an exit code."""
    want = declared()
    have = actual()

    if have is None:
        print(f"Could not reach {_ORG} - fleet NOT checked.")
        return 2

    missing = sorted(want - have)
    unlisted = sorted(have - want)

    if not missing and not unlisted:
        print(f"Fixture fleet matches the org ({len(want)} repos).")
        return 0

    if missing:
        print("Declared in config/fixtures.yaml but NOT in the org:")
        for name in missing:
            print(f"  - {name}")
        print("  Renamed or deleted. Fix the SSoT, or restore the repo.")
    if unlisted:
        print("In the org but NOT declared:")
        for name in unlisted:
            print(f"  - {name}")
        print("  Add it, or it will never be swept.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
