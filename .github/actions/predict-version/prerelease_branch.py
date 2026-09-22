#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/prerelease_branch.py
# Purpose:   Print whether this ref is a declared prerelease branch
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Print ``true`` when ``GITHUB_REF`` names a declared prerelease branch.

The gate decides whether a push releases, and it runs before
setup-semantic-release has put a config in the workspace. So the declaration
is read here from the same two places that composite chooses between: the
repo's own ``.releaserc*``, else the central ``default.releaserc.json`` in
the sibling action directory.

The composite runs in the caller's job, where hyperi-ci is not installed, so
the implementation is loaded out of the action's own checkout -- the same
by-path approach ``release_worthy.py`` and ``seed_version.py`` take, and the
reason ``src/hyperi_ci/release_branches.py`` is stdlib-only.

Prints ``false`` on ANY failure. Answering ``true`` for a branch the config
that actually runs does not declare buys a release run that semantic-release
then rejects, so the unknown case validates instead. The gate warns when a
release trailer is ignored, so a false answer is never silent.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs this composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

_ACTION_DIR = Path(__file__).resolve().parent
_PACKAGE = _ACTION_DIR.parents[2] / "src" / "hyperi_ci"
_CENTRAL_CONFIG = (
    _ACTION_DIR.parent / "setup-semantic-release" / "default.releaserc.json"
)


def main() -> int:
    package = types.ModuleType("hyperi_ci")
    package.__path__ = [str(_PACKAGE)]
    sys.modules["hyperi_ci"] = package

    from hyperi_ci.release_branches import (
        is_prerelease_ref,
        resolve_prerelease_branches,
    )

    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or ".")
    branches = resolve_prerelease_branches(workspace, _CENTRAL_CONFIG)
    ref = os.environ.get("GITHUB_REF", "")
    declared = ", ".join(branches) if branches else "(none)"
    print(f"prerelease branches declared: {declared}", file=sys.stderr)
    print("true" if is_prerelease_ref(ref, branches) else "false")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"prerelease-branch check failed: {exc}", file=sys.stderr)
        print("false")
        sys.exit(0)
