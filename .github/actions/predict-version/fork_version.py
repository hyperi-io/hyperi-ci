#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/fork_version.py
# Purpose:   Predict a fork's next version from its first-parent commits
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Write ``first-parent`` and, for a fork, ``version`` to ``$GITHUB_OUTPUT``.

The decision lives in ``src/hyperi_ci/fork_version.py``. The composite runs in
the caller's job, where hyperi-ci is not installed, so the implementation is
loaded out of the action's own checkout, the same by-path approach
``arm64_check.py`` takes.

A repo that is not a fork writes ``first-parent=false`` and the semantic-release
steps predict as before. A failed classification check does the same with a
warning, because the release tail reads the classification again and an
over-counted bump is the behaviour every repo had before. A fork whose
first-parent history gives no version fails the step.
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


def _load_package() -> None:
    # hyperi_ci/__init__ reads installed package metadata, which is absent here.
    if "hyperi_ci" not in sys.modules:
        package = types.ModuleType("hyperi_ci")
        package.__path__ = [str(_PACKAGE)]
        sys.modules["hyperi_ci"] = package


def run() -> int:
    """Classify the repo and, for a fork, predict and write its version."""
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or ".")
    try:
        _load_package()
        from hyperi_ci.fork_version import check_fork

        check = check_fork(workspace)
    except Exception as exc:
        _write_outputs(**{"first-parent": "false"})
        print(
            f"::warning title=fork release::classification check failed ({exc}) -- versioning with semantic-release"
        )
        return 0

    if check.warning:
        print(
            f"::warning title=fork release::{check.warning} -- versioning with semantic-release"
        )
    if not check.fork:
        _write_outputs(**{"first-parent": "false"})
        print(f"first-parent versioning off: {check.reason}", file=sys.stderr)
        return 0

    try:
        from hyperi_ci.fork_version import predict_version

        version, how = predict_version(workspace)
    except Exception as exc:
        print(f"::error title=fork release::{exc}")
        return 1

    _write_outputs(**{"first-parent": "true", "version": version})
    print(f"::notice title=fork release::Predicted next version: v{version} -- {how}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
