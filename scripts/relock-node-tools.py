#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/relock-node-tools.py
# Purpose:   Regenerate the lockfile for the docs checks' npm packages
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Regenerate ``src/hyperi_ci/config/node-tools/package-lock.json``.

Run after bumping linkedom, markdownlint-cli2 or mermaid in ``versions.yaml``,
and commit the lock with the bump. The manifest comes from
:func:`hyperi_ci.quality.node_tools.manifest`, the same one the runtime install
renders, so the two cannot disagree.

Usage:
    uv run scripts/relock-node-tools.py
"""

import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hyperi_ci.channel import COOLDOWN_DAYS
from hyperi_ci.common import error, run_cmd, success
from hyperi_ci.quality.node_tools import LOCKFILE, manifest


def main() -> int:
    """Resolve the pinned set with npm and write its lockfile. Returns exit code."""
    npm = shutil.which("npm")
    if npm is None:
        error("npm is not on PATH")
        return 1
    with tempfile.TemporaryDirectory(prefix="hyperi-relock-") as tmp:
        work = Path(tmp)
        (work / "package.json").write_text(
            json.dumps(manifest(), indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        # --before holds the TRANSITIVE tree to the same soak as the direct pins;
        # without it a dependency published this morning lands in the lock.
        before = (datetime.now(UTC) - timedelta(days=COOLDOWN_DAYS)).date()
        result = run_cmd(
            [
                npm,
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                f"--before={before.isoformat()}",
            ],
            check=False,
            cwd=work,
        )
        if result.returncode != 0:
            error(f"npm install --package-lock-only exited {result.returncode}")
            return result.returncode
        LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work / "package-lock.json", LOCKFILE)
    success(f"wrote {LOCKFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
