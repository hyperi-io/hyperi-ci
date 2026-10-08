#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/install-test-tools.py
# Purpose:   Put lychee and alint on PATH for the real-binary unit tests
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install lychee and alint on a Linux CI runner for hyperi-ci's own tests.

Uses the quality stage's own pinned installer, so the tests run the exact
binaries the stage would, and appends their directories to ``$GITHUB_PATH``
for the steps after this one. Linux assets only.

Usage:  uv run --no-sources python scripts/install-test-tools.py
Exit 1 when either install fails.
"""

import os
import sys
from pathlib import Path

from hyperi_ci.native_tools import ci_binary


def main() -> int:
    """Install both tools. Returns an exit code."""
    bin_dirs: list[str] = []
    for name in ("lychee", "alint"):
        exe = ci_binary(name)
        if exe is None:
            print(f"{name} install failed")
            return 1
        print(f"{name} installed: {exe}")
        bin_dirs.append(str(Path(exe).parent))

    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with open(github_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.writelines(f"{d}\n" for d in bin_dirs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
