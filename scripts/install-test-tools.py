#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/install-test-tools.py
# Purpose:   Put lychee and alint on PATH for the real-binary unit tests
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install lychee and alint on a Linux CI runner for hyperi-ci's own tests.

Uses the quality stage's own pinned fetch-and-verify helpers, so the tests run
the exact binaries the stage would. Both helpers fetch Linux assets only.

Usage:  uv run --no-sources python scripts/install-test-tools.py
Exit 1 when either install fails.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hyperi_ci.quality.doc_links import _install_lychee
from hyperi_ci.quality.repo_advisor import _install_alint

ALINT_TARGET = "/usr/local/bin/alint"


def main() -> int:
    """Install both tools. Returns an exit code."""
    lychee = _install_lychee()
    if lychee is None:
        print("lychee install failed")
        return 1
    print(f"lychee installed: {lychee}")

    fetch_dir = Path(os.environ.get("RUNNER_TEMP") or tempfile.mkdtemp()) / "alint"
    fetch_dir.mkdir(parents=True, exist_ok=True)
    alint = _install_alint(fetch_dir)
    if alint is None:
        print("alint install failed")
        return 1
    # The helper leaves alint in its fetch dir, since the stage runs it by path.
    subprocess.run(
        ["sudo", "install", "-m", "0755", str(alint), ALINT_TARGET], check=True
    )
    print(f"alint installed: {ALINT_TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
