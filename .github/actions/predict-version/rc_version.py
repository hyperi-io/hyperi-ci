#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/rc_version.py
# Purpose:   Turn the predicted version into a release candidate where opted in
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Write ``version`` and ``candidate`` to ``$GITHUB_OUTPUT``.

Reads the predicted version from ``PLANNED_VERSION``. The decision lives in
``src/hyperi_ci/release_candidate.py``, loaded out of the action's own checkout
the way ``fork_version.py`` loads its module.

An unreadable project config fails the step: guessing ships either a GA nobody
promoted or a candidate nobody asked for.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs this composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[3] / "src" / "hyperi_ci"


def _write_outputs(**values: str) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(lines)
    else:
        sys.stdout.write(lines)


def run() -> int:
    """Resolve the release version and write it with the candidate flag."""
    planned = os.environ.get("PLANNED_VERSION", "").strip()
    if not planned:
        print("::error title=release candidate::no predicted version to resolve")
        return 1

    if "hyperi_ci" not in sys.modules:
        package = types.ModuleType("hyperi_ci")
        package.__path__ = [str(_PACKAGE)]
        sys.modules["hyperi_ci"] = package
    from hyperi_ci.release_candidate import hotfix_reason, repo_tags, resolve

    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or ".")
    try:
        decision = resolve(
            workspace, planned, repo_tags(workspace), hotfix_reason(workspace)
        )
    except ValueError as exc:
        print(f"::error title=release candidate::{exc}")
        return 1

    _write_outputs(
        version=decision.version,
        candidate="true" if decision.candidate else "false",
    )
    level = "warning" if decision.warning else "notice"
    print(
        f"::{level} title=release candidate::v{decision.version} -- {decision.reason}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
