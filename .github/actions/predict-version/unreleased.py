#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/unreleased.py
# Purpose:   Print the warning when HEAD carries unreleased releasable work
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Print the warning text when a validate-only run leaves a release waiting.

A push to main with no release trailer validates and publishes nothing, which
is correct -- and indistinguishable from a release run at the rollup. scalo-rs
sat 13 releasable commits and 26 days behind crates.io, one of them a security
floor bump, and every run said SUCCESS.

Stdout carries the warning text and is EMPTY when there is nothing to warn
about; the caller raises whatever it gets as ``::warning::``. Stderr always
says which of the three answers this was, so a quiet run still records why it
was quiet.

Loaded the same way as ``release_worthy.py``: the composite runs in the
caller's job where hyperi-ci is not installed, so ``hyperi_ci`` is registered
as a package pointing at ``src/hyperi_ci`` and the submodule imported by path.
``hyperi_ci/__init__`` reads installed package metadata and would raise here.

Prints nothing and exits 0 on ANY failure. This decides only whether a warning
appears, so a crash must never take the release gate down with it.
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

    from hyperi_ci.commit_range import unreleased_warning

    warn, message = unreleased_warning()
    if warn:
        print(message)
    else:
        print(message, file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"unreleased-work check failed: {exc}", file=sys.stderr)
        sys.exit(0)
