# Project:   HyperI CI
# File:      src/hyperi_ci/upgrade.py
# Purpose:   Self-upgrade and auto-update logic
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
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

from packaging.version import InvalidVersion, Version
from scalo.logger import logger

from hyperi_ci import __version__
from hyperi_ci.common import is_ci

PACKAGE = "hyperi-ci"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
PYPI_TIMEOUT = 5
SUBPROCESS_TIMEOUT = 30
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

    The unpinned uv path uses ``tool install --force ...@latest`` rather than
    ``tool upgrade``. ``tool upgrade`` refuses to act on an install whose receipt
    carries an exact version, and refusing is how it reports that -- exit 0 with
    "Nothing to upgrade". So one ``upgrade --version`` in the past would have
    disabled every upgrade after it. ``@latest`` both moves the version and
    clears the pin, which is uv's own advice in that message.

    ``--refresh`` is on both uv paths because ``@latest`` resolves against uv's
    CACHED index, not PyPI. Observed on the 2.9.6 release: PyPI served 2.9.6 while
    ``tool install --force ...@latest`` installed 2.9.5 from cache, and it took
    ``--refresh`` to see it. Auto-update reads the real latest from the PyPI JSON
    API, so without this it asks uv for a version uv does not yet believe in --
    which now warns rather than lying, but should not happen at all. The pinned
    path needs it for the same reason: a version released moments ago is not in
    the cached index either.

    pip has no index-only refresh -- ``--no-cache-dir`` would also throw away the
    wheel cache -- so the pip path is left alone. If it serves stale metadata the
    post-check catches it.

    Args:
        uv_path: Path to uv binary, or None to use pip.
        version: Specific version to install, or None for latest.
        pre: Include pre-releases when resolving latest.

    Returns:
        Command as list of strings.

    """
    if uv_path:
        cmd = [uv_path, "tool", "install", "--force", "--refresh"]
        if version:
            cmd.append(f"{PACKAGE}=={version}")
            return cmd
        if pre:
            cmd.append("--prerelease=allow")
        cmd.append(f"{PACKAGE}@latest")
        return cmd

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if pre and not version:
        cmd.append("--pre")
    pkg = f"{PACKAGE}=={version}" if version else PACKAGE
    cmd.append(pkg)
    return cmd


def _parse_installed_version(output: str, *, from_uv: bool) -> str | None:
    """Pull the installed hyperi-ci version out of uv or pip output.

    Args:
        output: stdout of ``uv tool list`` or ``pip show hyperi-ci``.
        from_uv: True when parsing uv output, False for pip.

    Returns:
        Version string, or None if the package was not found in the output.

    """
    for raw in output.splitlines():
        line = raw.strip()
        if from_uv:
            # `uv tool list` prints one line per tool -- "hyperi-ci v2.9.5" --
            # followed by its entrypoints indented with "- ". Only the tool line
            # carries the version, and the entrypoint shares the tool's name, so
            # the "v" separator is what distinguishes them.
            if line.startswith(f"{PACKAGE} v"):
                return line.split(" v", 1)[1].strip()
        elif line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def _installed_version(uv_path: str | None) -> str | None:
    """Read the version installed on disk, not the one currently running.

    A process that upgrades itself still has the old ``__version__`` imported, so
    it cannot use that to confirm anything landed. Ask the installer.

    Args:
        uv_path: Path to uv binary, or None to ask pip.

    Returns:
        Installed version string, or None if it could not be determined.

    """
    cmd = (
        [uv_path, "tool", "list"]
        if uv_path
        else [sys.executable, "-m", "pip", "show", PACKAGE]
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_installed_version(result.stdout, from_uv=bool(uv_path))


def _confirm_upgraded(uv_path: str | None, target: str) -> bool:
    """Check the installed version actually reached target.

    Exit code 0 is not evidence of an upgrade. ``uv tool upgrade`` exits 0 when it
    declines to do anything, so the old code logged a version bump that had not
    happened, wrote the freshness timestamp, and suppressed the next check.

    Args:
        uv_path: Path to uv binary, or None for pip.
        target: Version the upgrade was aiming at.

    Returns:
        True if the installed version is now at or past target.

    """
    installed = _installed_version(uv_path)
    if installed is None:
        logger.warning(
            "Upgrade command succeeded but the installed version could not be "
            "read, so the upgrade is unconfirmed"
        )
        return False
    try:
        moved = Version(installed) >= Version(target)
    except InvalidVersion:
        logger.warning(f"Installed version is not parseable: {installed}")
        return False
    if not moved:
        logger.warning(
            f"Upgrade did not take effect -- still on {installed}, wanted {target}. "
            f"If this install is pinned, reinstall with: "
            f"uv tool install --force --refresh {PACKAGE}@latest"
        )
    return moved


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
        with urllib.request.urlopen(req, timeout=PYPI_TIMEOUT) as resp:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected — hardcoded PyPI HTTPS URL
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

    if not _confirm_upgraded(uv_path, target):
        return 1

    if version and uv_path:
        # An explicit --version is a deliberate act, so honour it -- but say out
        # loud what it costs, because uv records it as an exact pin and every
        # later auto-update will decline without explaining why.
        logger.warning(
            f"Pinned to {target}. Auto-updates will not move off it. "
            f"To go back to tracking latest: hyperi-ci upgrade"
        )

    logger.info(f"{PACKAGE} upgraded: {current} -> {target}")
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

        if not _confirm_upgraded(uv_path, stable):
            # Deliberately no timestamp. Writing one here is what let a stuck
            # install go quiet for four hours at a time, so leave the check due
            # and let the next invocation try again and warn again.
            return

        _write_timestamp()
        logger.info(f"{PACKAGE} upgraded: {current} -> {stable}")
        _re_exec()

    except Exception as exc:
        logger.warning(f"Auto-update check failed: {exc}")
