# Project:   HyperI CI
# File:      src/hyperi_ci/upgrade.py
# Purpose:   Self-upgrade and auto-update logic
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Self-upgrade functionality for hyperi-ci CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from packaging.version import Version

from hyperi_ci import __version__
from hyperi_ci.common import is_ci
from hyperi_pylib.logger import logger

PYPI_URL = "https://pypi.org/pypi/hyperi-ci/json"
PYPI_TIMEOUT = 5
CACHE_DIR = Path.home() / ".cache" / "hyperi-ci"
TIMESTAMP_FILE = CACHE_DIR / "last-update-check"
CHECK_INTERVAL = 4 * 60 * 60  # 4 hours in seconds


def _parse_latest_version(
    releases: dict[str, list],
) -> tuple[str | None, str | None]:
    """Parse latest stable and pre-release versions from PyPI releases dict.

    Args:
        releases: PyPI releases mapping {version_string: [file_dicts]}.

    Returns:
        Tuple of (latest_stable, latest_prerelease). Either may be None.
    """
    stable: list[Version] = []
    all_versions: list[Version] = []

    for ver_str, files in releases.items():
        if not files:
            continue
        try:
            v = Version(ver_str)
        except Exception:
            continue
        all_versions.append(v)
        if not v.is_prerelease and not v.is_devrelease:
            stable.append(v)

    latest_stable = str(max(stable)) if stable else None
    latest_pre = str(max(all_versions)) if all_versions else None
    return latest_stable, latest_pre


def _build_upgrade_cmd(
    *,
    uv_path: str | None,
    version: str | None,
    pre: bool,
) -> list[str]:
    """Build the subprocess command for upgrading hyperi-ci.

    Args:
        uv_path: Path to uv binary, or None to use pip.
        version: Specific version to install, or None for latest.
        pre: Include pre-releases when resolving latest.

    Returns:
        Command as list of strings.
    """
    if uv_path:
        if version:
            return [uv_path, "tool", "install", "--force", f"hyperi-ci=={version}"]
        cmd = [uv_path, "tool", "upgrade"]
        if pre:
            cmd.append("--prerelease=allow")
        cmd.append("hyperi-ci")
        return cmd

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if pre and not version:
        cmd.append("--pre")
    pkg = f"hyperi-ci=={version}" if version else "hyperi-ci"
    cmd.append(pkg)
    return cmd


def _timestamp_age() -> float:
    """Return age of timestamp file in seconds, or infinity if missing."""
    try:
        ts = float(TIMESTAMP_FILE.read_text().strip())
        return time.time() - ts
    except (FileNotFoundError, ValueError):
        return float("inf")


def _write_timestamp() -> None:
    """Write current time to the timestamp file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TIMESTAMP_FILE.write_text(str(time.time()))


def _should_auto_update() -> bool:
    """Check all gates for auto-update.

    Returns False if any gate blocks the update.
    """
    # Recursion guard
    if os.environ.get("_HYPERCI_UPGRADING") == "1":
        return False

    # Skip when the user is running "upgrade" explicitly
    if len(sys.argv) >= 2 and sys.argv[1] == "upgrade":
        return False

    # Explicit env var override (takes precedence over CI detection)
    auto_update_env = os.environ.get("HYPERCI_AUTO_UPDATE", "").lower()
    if auto_update_env == "false":
        return False
    if auto_update_env == "true":
        pass  # Explicit opt-in, skip CI check
    elif is_ci():
        return False

    # Timestamp check
    if _timestamp_age() < CHECK_INTERVAL:
        return False

    return True


def _fetch_pypi_versions() -> tuple[str | None, str | None]:
    """Fetch latest stable and pre-release versions from PyPI.

    Returns:
        Tuple of (latest_stable, latest_prerelease). Both None on error.
    """
    try:
        req = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=PYPI_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return _parse_latest_version(data.get("releases", {}))
    except Exception:
        return None, None
