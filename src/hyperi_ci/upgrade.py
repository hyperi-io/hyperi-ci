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

from hyperi_pylib.logger import logger
from packaging.version import Version

from hyperi_ci import __version__
from hyperi_ci.common import is_ci

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
        with urllib.request.urlopen(req, timeout=PYPI_TIMEOUT) as resp:  # nosec B310 — hardcoded PyPI HTTPS URL
            data = json.loads(resp.read())
        return _parse_latest_version(data.get("releases", {}))
    except Exception:
        return None, None


def _run_upgrade_cmd(cmd: list[str]) -> int:
    """Run the upgrade subprocess with graceful error handling.

    Handles permission errors, missing binaries, and other OS-level
    failures so the caller can decide whether to continue or abort.

    Returns:
        Exit code (0 = success, non-zero = failure).

    """
    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except PermissionError:
        logger.warning(
            "Permission denied — try running with sudo or fix install permissions"
        )
        return 1
    except FileNotFoundError as exc:
        logger.warning(f"Command not found: {exc}")
        return 1
    except OSError as exc:
        logger.warning(f"Upgrade command failed: {exc}")
        return 1


def _re_exec() -> None:
    """Replace current process with a fresh invocation of the same command."""
    env = os.environ.copy()
    env["_HYPERCI_UPGRADING"] = "1"
    try:
        os.execvpe(sys.argv[0], sys.argv, env)
    except OSError:
        logger.warning("Upgrade installed but re-exec failed — run your command again")
        raise SystemExit(0)


def run_upgrade(
    version: str | None = None,
    pre: bool = False,
) -> int:
    """Run an explicit upgrade.

    Args:
        version: Specific version to install, or None for latest.
        pre: Include pre-releases.

    Returns:
        Exit code (0 = success).

    """
    # Resolve target version
    if version:
        target = version
    else:
        stable, prerelease = _fetch_pypi_versions()
        target = prerelease if pre else stable
        if target is None:
            logger.error("Could not determine latest version from PyPI")
            return 1

    current = Version(__version__)
    try:
        target_ver = Version(target)
    except Exception:
        logger.error(f"Invalid version: {target}")
        return 1

    if current == target_ver:
        logger.info(f"Already up to date ({current})")
        return 0

    # Build and run upgrade command
    uv_path = shutil.which("uv")
    cmd = _build_upgrade_cmd(
        uv_path=uv_path,
        version=target if version else None,
        pre=pre,
    )
    logger.info(f"Upgrading: {' '.join(cmd)}")

    rc = _run_upgrade_cmd(cmd)
    if rc != 0:
        logger.error(f"Upgrade failed (exit {rc})")
        return rc

    logger.info(f"hyperi-ci upgraded: {current} -> {target}")
    _re_exec()
    return 0  # unreachable after execvpe, keeps type checker happy


def maybe_auto_update() -> None:
    """Check for updates and auto-upgrade if appropriate.

    Called from the CLI app callback. Never raises — all errors are
    caught and logged as warnings so the original command proceeds.
    """
    try:
        if not _should_auto_update():
            return

        stable, _ = _fetch_pypi_versions()
        if stable is None:
            return

        current = Version(__version__)
        latest = Version(stable)
        if current >= latest:
            _write_timestamp()
            return

        # Upgrade needed
        uv_path = shutil.which("uv")
        cmd = _build_upgrade_cmd(uv_path=uv_path, version=None, pre=False)

        rc = _run_upgrade_cmd(cmd)
        if rc != 0:
            logger.warning(f"Auto-update failed (exit {rc})")
            return

        _write_timestamp()
        logger.info(f"hyperi-ci upgraded: {current} -> {stable}")
        _re_exec()

    except Exception as exc:
        logger.warning(f"Auto-update check failed: {exc}")
