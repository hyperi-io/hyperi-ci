#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/touched_files.py
# Purpose:   Write whether the pushed range changed anything but docs
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Write ``code-changed`` and ``touched`` to ``$GITHUB_OUTPUT``.

``code-changed`` is ``true`` when the pushed range changed any file outside
docs, images and repo metadata, so a ``chore:`` merge that edits code runs
quality and test (issue #96). ``touched`` is the one line saying why, which the
gate's skipped-checks notice quotes. The decision is
``hyperi_ci.commit_range.touches_code``.

Loaded the same way as ``release_worthy.py``: the composite runs in the
caller's job where hyperi-ci is not installed, so ``hyperi_ci`` is registered
as a package pointing at ``src/hyperi_ci`` and the submodule imported by path.

Writes ``code-changed=true`` on ANY failure. This decides whether quality and
test run at all, so a helper that crashes must open the gate rather than close
it (design principle 3 -- no silent skips).
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


def _write_outputs(changed: bool, reason: str) -> None:
    # A step output ends at the first newline, and a path may carry one.
    reason = " ".join(reason.splitlines())
    lines = f"code-changed={'true' if changed else 'false'}\ntouched={reason}\n"
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(lines)
    else:
        sys.stdout.write(lines)


def main() -> int:
    package = types.ModuleType("hyperi_ci")
    package.__path__ = [str(_PACKAGE)]
    sys.modules["hyperi_ci"] = package

    from hyperi_ci.commit_range import touches_code

    changed, reason = touches_code()
    print(f"changed files: {reason}", file=sys.stderr)
    _write_outputs(changed, reason)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"changed-files check failed: {exc}", file=sys.stderr)
        _write_outputs(
            True, f"the changed-files check failed ({exc}) -- running the checks"
        )
        sys.exit(0)
