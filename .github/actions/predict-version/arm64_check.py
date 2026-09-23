#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/arm64_check.py
# Purpose:   Print whether this project owes an arm64 build on a main merge
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Print ``true`` when this project owes an arm64 build for parity.

The gate calls this only for a release-worthy push to main; the project half of
the decision -- does this repo build aarch64, and has it opted out -- lives in
``src/hyperi_ci/arm64_check.py``.

The composite runs in the caller's job, where hyperi-ci is not installed, so
the implementation is loaded out of the action's own checkout, the same by-path
approach ``release_worthy.py`` and ``prerelease_branch.py`` take.

Prints ``false`` on ANY failure. A missed arm64 check costs one build's worth
of coverage; a wrong ``true`` spends an arm64 runner on every releasable merge
in a repo that asked for neither. The reason goes to stderr either way, and the
gate's summary line carries the value.
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


def main() -> int:
    package = types.ModuleType("hyperi_ci")
    package.__path__ = [str(_PACKAGE)]
    sys.modules["hyperi_ci"] = package

    from hyperi_ci.arm64_check import wants_arm64_check

    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or ".")
    wanted, reason = wants_arm64_check(workspace)
    print(f"arm64 check: {reason}", file=sys.stderr)
    print("true" if wanted else "false")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"arm64 check failed: {exc}", file=sys.stderr)
        print("false")
        sys.exit(0)
