#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/python_version.py
# Purpose:   Print the Python version a project builds on, with no install
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Print the Python version this project should be built and tested on.

The composite runs in the caller's job, where hyperi-ci is not installed, so
the implementation is loaded out of the action's own checkout -- the same
by-path approach ``seed_version.py`` takes, and the reason
``src/hyperi_ci/python_version.py`` is stdlib-only.

Prints the fallback on ANY failure. Every downstream job installs whatever this
prints, so a helper that crashes must still name a usable interpreter rather
than leave the version empty and fail four jobs later.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _REPO_ROOT / "src" / "hyperi_ci" / "python_version.py"


def main() -> int:
    requested = os.environ.get("REQUESTED_PYTHON", "")
    fallback = os.environ.get("DEFAULT_PYTHON", "")

    spec = importlib.util.spec_from_file_location("hyperi_ci_python_version", _MODULE)
    if spec is None or spec.loader is None:
        print(f"cannot load {_MODULE}", file=sys.stderr)
        print(fallback or requested)
        return 0
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    version, source = module.resolve(Path.cwd(), requested=requested, default=fallback)
    print(f"python {version or '(unresolved)'} (from {source})", file=sys.stderr)
    print(version)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"python-version resolution failed: {exc}", file=sys.stderr)
        print(os.environ.get("DEFAULT_PYTHON", ""))
        sys.exit(0)
