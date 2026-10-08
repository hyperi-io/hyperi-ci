#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/relock-node-tools.py
# Purpose:   Regenerate the lockfile for the docs checks' npm packages
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Regenerate ``src/hyperi_ci/config/node-tools/package-lock.json``.

Run after bumping linkedom, markdownlint-cli2 or mermaid in ``versions.yaml``,
and commit the lock with the bump. ``--auto-update`` makes the bump as well: it
moves every lock-pinned tool to its newest release past the cooldown, relocks,
and restores ``versions.yaml`` if the relock fails. The manifest comes from
:func:`hyperi_ci.quality.node_tools.manifest`, the same one the runtime install
renders, so the two cannot disagree.

Usage:
    uv run scripts/relock-node-tools.py                # relock the current pins
    uv run scripts/relock-node-tools.py --auto-update  # bump the pins, then relock
"""

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from hyperi_ci import versions
from hyperi_ci.channel import COOLDOWN_DAYS
from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.quality.node_tools import LOCKFILE, manifest

# update-versions.py owns release resolution and the comment-preserving SSOT edit.
_SPEC = importlib.util.spec_from_file_location(
    "update_versions", Path(__file__).resolve().parent / "update-versions.py"
)
assert _SPEC is not None and _SPEC.loader is not None  # a real file always resolves
update_versions = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(update_versions)


def _relock(before: date) -> int:
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


def _auto_update(today: datetime) -> int:
    """Bump every lock-pinned tool past the cooldown, then relock. Returns exit code.

    The cooldown is measured from ``today`` at midnight, the same instant npm's
    date-only ``--before`` resolves to, so every bumped pin is one the relock
    can see.
    """
    original = versions.VERSIONS_FILE.read_text(encoding="utf-8")
    tools = (yaml.safe_load(original) or {}).get("tools") or {}
    bumps: dict[str, str] = {}
    unchecked: list[str] = []
    for name, spec in tools.items():
        if not isinstance(spec, dict) or not spec.get("lockfile"):
            continue
        latest, status = update_versions._latest_tool_release(spec, today)
        if status == "ok" and latest:
            info(f"  {name}: {spec.get('version')} -> {latest}")
            bumps[name] = latest
        elif status == "lookup-failed":
            # A tool we could not reach is not a tool that is current.
            warn(f"  {name}: {spec.get('version')} (COULD NOT CHECK -- not bumped)")
            unchecked.append(name)
    if not bumps:
        info("No lock-pinned tool has a newer release past the cooldown")
        return 1 if unchecked else 0

    text = original
    for name, version in bumps.items():
        text = update_versions._set_tool_version_in_yaml(text, name, version)
    versions.VERSIONS_FILE.write_text(text, encoding="utf-8", newline="\n")
    # manifest() reads the SSOT through a cached loader; make it see the bumps.
    versions._data.cache_clear()

    rc = 1
    try:
        rc = _relock((today - timedelta(days=COOLDOWN_DAYS)).date())
    finally:
        if rc != 0:
            versions.VERSIONS_FILE.write_text(original, encoding="utf-8", newline="\n")
            versions._data.cache_clear()
            error("relock failed, so versions.yaml is back to its pins")
    if rc != 0:
        return rc
    return 1 if unchecked else 0


def main() -> int:
    """Parse the flags, then relock or bump-and-relock. Returns exit code."""
    parser = argparse.ArgumentParser(
        description="Regenerate the node-tools lockfile from versions.yaml",
    )
    parser.add_argument(
        "--auto-update",
        action="store_true",
        help="Bump every lock-pinned tool past the cooldown first, then relock",
    )
    args = parser.parse_args()
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.auto_update:
        return _auto_update(today)
    return _relock((today - timedelta(days=COOLDOWN_DAYS)).date())


if __name__ == "__main__":
    sys.exit(main())
