#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/install-test-tools.py
# Purpose:   Put lychee and alint on PATH for the real-binary unit tests
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install lychee and alint on a Linux CI runner for hyperi-ci's own tests.

Uses the quality stage's own pinned installer, so the tests run the exact
binaries the stage would. A directory it installed into is appended to
``$GITHUB_PATH`` for the steps after this one; a copy already on PATH is left
where it is, so the order of PATH for those steps does not change. Linux
assets only.

Usage:  uv run --no-sources python scripts/install-test-tools.py
Exit 1 when either install fails.
"""

import os
import sys
from pathlib import Path

from hyperi_ci.common import error, info
from hyperi_ci.native_tools import ci_binary, install_root


def main() -> int:
    """Install both tools. Returns an exit code."""
    cache = install_root().resolve()
    bin_dirs: list[str] = []
    for name in ("lychee", "alint"):
        exe = ci_binary(name)
        if exe is None:
            error(f"{name} install failed")
            return 1
        info(f"{name} installed: {exe}")
        bin_dir = Path(exe).parent
        if bin_dir.resolve().is_relative_to(cache):
            bin_dirs.append(str(bin_dir))

    github_path = os.environ.get("GITHUB_PATH")
    if github_path and bin_dirs:
        with open(github_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.writelines(f"{d}\n" for d in bin_dirs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
