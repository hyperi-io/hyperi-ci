#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/seed_version.py
# Purpose:   Print a tag-less repo's starting version, with no install
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Print the version a tag-less repo starts from, for the composite action.

The composite runs in the caller's job, where hyperi-ci is not installed --
the plan job deliberately installs nothing heavier than semantic-release. But
the action checkout IS a full hyperi-ci checkout, so the real implementation
is three directories up. Load it by path: same code as
``hyperi-ci seed-version``, no duplicated version logic, no pip install.

``src/hyperi_ci/version_source.py`` is stdlib-only for exactly this reason.
Importing it as a package member instead would drag in the whole dependency
tree via ``hyperi_ci/__init__``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _REPO_ROOT / "src" / "hyperi_ci" / "version_source.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("hyperi_ci_version_source", _MODULE)
    if spec is None or spec.loader is None:
        print(f"cannot load {_MODULE}", file=sys.stderr)
        return 1
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    version, source = module.seed_version(Path.cwd())
    print(f"seed version {version} ({source})", file=sys.stderr)
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
