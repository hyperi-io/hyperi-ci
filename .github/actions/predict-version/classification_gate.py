#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/classification_gate.py
# Purpose:   Refuse a publish the repo's declared classification forbids
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Exit 1 when the repo's declared classification forbids publishing.

The gate step runs this before it writes ``will-publish=true``, so a refusal
fails Plan and nothing is tagged, pushed or uploaded. The decision lives in
``src/hyperi_ci/classification.py``, loaded by path like ``fork_version.py``.

``REPO_PRIVATE`` carries ``github.event.repository.private``. A misspelt
classification refuses, because the publish would otherwise go ahead on a
marker nobody can read. Any other failure warns and lets the publish run.
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

_TITLE = "classification"


def _load_package() -> None:
    # hyperi_ci/__init__ reads installed package metadata, which is absent here.
    if "hyperi_ci" not in sys.modules:
        package = types.ModuleType("hyperi_ci")
        package.__path__ = [str(_PACKAGE)]
        sys.modules["hyperi_ci"] = package


def run() -> int:
    """Check the declared classification and say whether this run may publish."""
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or ".")
    private = os.environ.get("REPO_PRIVATE", "") == "true"
    try:
        _load_package()
        from hyperi_ci.classification import publish_refusal, resolve_checkout

        resolution, unreadable = resolve_checkout(workspace)
    except ValueError as exc:
        print(f"::error title={_TITLE}::{exc} Nothing is tagged or published.")
        return 1
    except Exception as exc:
        print(
            f"::warning title={_TITLE}::classification check failed ({exc}) -- publishing as undeclared"
        )
        return 0

    if unreadable:
        print(
            f"::warning title={_TITLE}::{unreadable}, so a classification declared there is not seen"
        )
    refusal = publish_refusal(resolution, private)
    if refusal:
        print(
            f"::error title={_TITLE}::{refusal}. Nothing is tagged or published. "
            "Change the declaration in .hyperi-ci.yaml or .hyperi-classification "
            "if this repo should publish."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
