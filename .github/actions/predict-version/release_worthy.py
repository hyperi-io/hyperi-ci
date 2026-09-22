#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/release_worthy.py
# Purpose:   Print whether the pushed range ships a release, with no install
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Print ``true`` when the commit range this push introduced is release-worthy.

The composite runs in the caller's job, where hyperi-ci is not installed, so
the implementation is loaded out of the action's own checkout -- the same
by-path approach ``seed_version.py`` takes, and the reason
``src/hyperi_ci/commit_range.py`` imports nothing heavier than
``release_rules``.

``hyperi_ci/__init__`` reads installed package metadata and would raise here,
so the name is registered as a package pointing at ``src/hyperi_ci`` before
the import: the submodules load without the real ``__init__`` running.

Prints ``true`` on ANY failure. This answer decides whether quality and test
run at all, so a helper that crashes must open the gate rather than close it
(design principle 3 -- no silent skips).
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs this composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

import sys
import types
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[3] / "src" / "hyperi_ci"


def main() -> int:
    package = types.ModuleType("hyperi_ci")
    package.__path__ = [str(_PACKAGE)]
    sys.modules["hyperi_ci"] = package

    from hyperi_ci.commit_range import is_release_worthy

    worthy, reason = is_release_worthy()
    print(reason, file=sys.stderr)
    print("true" if worthy else "false")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"release-worthiness check failed: {exc}", file=sys.stderr)
        print("true")
        sys.exit(0)
