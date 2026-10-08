# Project:   HyperI CI
# File:      src/hyperi_ci/upgrade.py
# Purpose:   Self-upgrade and auto-update logic
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Self-upgrade functionality for hyperi-ci CLI.

The channel picks the target release (see :mod:`hyperi_ci.channel`): ``live``
takes the newest release on PyPI and ``stable`` the newest one past the
cooldown. Building the installer command, confirming the version moved and
re-exec are the same on both.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from packaging.version import InvalidVersion, Version
from scalo.logger import logger

from hyperi_ci import __version__, channel
from hyperi_ci.common import URL_ATTEMPTS, is_ci, url_read

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
        except InvalidVersion:
            continue
        all_versions.append(v)
        if not v.is_prerelease and not v.is_devrelease:
            stable.append(v)

    latest_stable = str(max(stable)) if stable else None
    latest_pre = str(max(all_versions)) if all_versions else None
    return latest_stable, latest_pre


def _release_upload_time(files: list[dict]) -> float | None:
    """Return when a release first became installable, as a unix timestamp.

    The earliest file wins, as a release is installable from its first upload.
    PyPI's older ``upload_time`` field carries no offset and would be read as
    local time, so a missing offset is filled in as UTC.

    Args:
        files: PyPI file entries for one release.

    Returns:
        Unix timestamp, or None when no entry carries a parseable time.

    """
    stamps: list[float] = []
    for entry in files:
        raw = entry.get("upload_time_iso_8601") or entry.get("upload_time")
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            stamps.append(
                (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
            )
        except ValueError:
            continue
    return min(stamps) if stamps else None


def _stable_releases_newest_first(
    releases: dict[str, list],
) -> list[tuple[Version, float | None]]:
    """Return (version, upload time) for real releases, newest version first.

    Pre-releases and dev-releases are dropped.

    Args:
        releases: PyPI releases mapping {version_string: [file_dicts]}.

    Returns:
        List of (version, upload timestamp or None), newest version first.

    """
    found: list[tuple[Version, float | None]] = []
    for ver_str, files in releases.items():
        if not files:
            continue
        try:
            version = Version(ver_str)
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease:
            continue
        found.append((version, _release_upload_time(files)))
    return sorted(found, key=lambda pair: pair[0], reverse=True)


def _soaked_version(
    releases: dict[str, list],
    *,
    cooldown_days: int = channel.COOLDOWN_DAYS,
    now: float | None = None,
) -> str | None:
    """Return the newest release aged past the cooldown, or None if none has.

    The first release old enough wins. One with an unreadable upload time is
    skipped (fail closed), so it holds the channel back instead of advancing it.

    Args:
        releases: PyPI releases mapping.
        cooldown_days: Days a release must have been installable.
        now: Unix timestamp to age against (tests); defaults to the clock.

    Returns:
        Version string, or None when nothing qualifies.

    """
    moment = time.time() if now is None else now
    for version, uploaded in _stable_releases_newest_first(releases):
        if uploaded is None:
            continue
        if (moment - uploaded) / 86400.0 >= cooldown_days:
            return str(version)
    return None


def _newest_with_age(
    releases: dict[str, list],
    *,
    now: float | None = None,
) -> tuple[str, float] | None:
    """Return (newest release, its age in days), for reporting only.

    Names what ``stable`` is waiting on when :func:`_soaked_version` falls short
    of it. The adoption decision stays with :func:`_soaked_version`.

    Args:
        releases: PyPI releases mapping.
        now: Unix timestamp to age against (tests); defaults to the clock.

    Returns:
        Tuple of (version string, age in days), or None when none is datable.

    """
    moment = time.time() if now is None else now
    for version, uploaded in _stable_releases_newest_first(releases):
        if uploaded is not None:
            return str(version), (moment - uploaded) / 86400.0
    return None


class UpgradeTarget(NamedTuple):
    """What a channel resolved to, and how to install it.

    Attributes:
        version: Release to install, or None when the channel has nothing to
            offer (no release has soaked yet, or PyPI could not be read).
        pin: Install the exact version rather than ``@latest``. True only while
            ``stable`` lags the newest release, as an exact specifier lands in
            uv's receipt as a pin.
        note: What the channel is holding out on, for logging, else None.

    """

    version: str | None
    pin: bool
    note: str | None


def _resolve_target(
    releases: dict[str, list],
    *,
    channel_name: str,
    pre: bool = False,
    now: float | None = None,
) -> UpgradeTarget:
    """Resolve which release the channel wants installed.

    ``pre`` resolves as ``live`` whatever the channel, as a pre-release has not
    soaked by definition.

    Args:
        releases: PyPI releases mapping.
        channel_name: "live" or "stable".
        pre: Include pre-releases when resolving.
        now: Unix timestamp to age against (tests); defaults to the clock.

    Returns:
        The resolved :class:`UpgradeTarget`.

    """
    latest_stable, latest_pre = _parse_latest_version(releases)
    if pre:
        return UpgradeTarget(version=latest_pre, pin=False, note=None)
    if channel_name != "stable":
        return UpgradeTarget(version=latest_stable, pin=False, note=None)

    soaked = _soaked_version(releases, now=now)
    newest = _newest_with_age(releases, now=now)
    note = None
    if newest is not None and newest[0] != soaked:
        remaining = channel.COOLDOWN_DAYS - newest[1]
        note = (
            f"stable is holding at {soaked or 'nothing yet'}: "
            f"{newest[0]} is {newest[1]:.1f} days old, "
            f"adopted in {max(remaining, 0):.1f} days"
        )
    # A receipt pin exists only while stable lags.
    pin = soaked is not None and soaked != latest_stable
    return UpgradeTarget(version=soaked, pin=pin, note=note)


def _build_upgrade_cmd(
    *,
    uv_path: str | None,
    version: str | None,
    pre: bool,
) -> list[str]:
    """Build the subprocess command for upgrading hyperi-ci.

    The unpinned uv path uses ``tool install --force ...@latest`` rather than
    ``tool upgrade``, which exits 0 with "Nothing to upgrade" on an install whose
    receipt carries an exact version, so one ``upgrade --version`` would disable
    every upgrade after it. ``@latest`` moves the version and clears the pin.

    ``--refresh`` is on both uv paths because ``@latest`` resolves against uv's
    CACHED index, which can lag PyPI (2.9.6 was live while uv installed 2.9.5).
    Auto-update reads the latest from the PyPI JSON API, so a stale index asks
    uv for a version it does not yet believe in. A pinned version released
    moments ago is missing from the cached index too.

    pip has no index-only refresh (``--no-cache-dir`` also drops the wheel
    cache), so the pip path is left alone and the post-check catches stale
    metadata.

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
            # The tool line ("hyperi-ci v2.9.5") carries the version. Its
            # indented entrypoint shares the name, so the "v" tells them apart.
            if line.startswith(f"{PACKAGE} v"):
                return line.split(" v", 1)[1].strip()
        elif line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def _installed_version(uv_path: str | None) -> str | None:
    """Read the version installed on disk, not the one currently running.

    A process that upgrades itself still has the old ``__version__`` imported,
    so this asks the installer.

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

    Exit code 0 is not evidence: ``uv tool upgrade`` exits 0 when it declines to
    act, which would log a bump that did not happen and suppress the next check.

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


def _effective_current(uv_path: str | None) -> Version:
    """Return the newer of the running version and the one on disk.

    They diverge for a source checkout and for one invocation after an upgrade.
    The newer of the two stops a channel whose target is older than the
    installed tool (``stable`` during its soak lag) from downgrading it.

    Args:
        uv_path: Path to uv binary, or None to ask pip.

    Returns:
        The higher of the two versions; the running one when disk is unreadable.

    """
    running = Version(__version__)
    installed = _installed_version(uv_path)
    if installed is None:
        return running
    try:
        return max(running, Version(installed))
    except InvalidVersion:
        return running


def _timestamp_age() -> float:
    """Return age of timestamp file in seconds, or infinity if missing."""
    try:
        ts = float(TIMESTAMP_FILE.read_text(encoding="utf-8").strip())
        return time.time() - ts
    except (FileNotFoundError, ValueError):
        return float("inf")


def _write_timestamp() -> None:
    """Write current time to the timestamp file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TIMESTAMP_FILE.write_text(str(time.time()), encoding="utf-8", newline="\n")


def _should_auto_update() -> bool:
    """Return False if any gate blocks auto-update."""
    return _blocking_gate() is None


def _blocking_gate(*, ignore_invocation: bool = False) -> str | None:
    """Name the first gate that blocks auto-update, or None when none does.

    One list of gates in precedence order, so ``autoupdate status`` reports the
    decision the callback makes.

    Args:
        ignore_invocation: Skip the recursion guard and the explicit-command
            gate, which say nothing about the machine's configuration.

    Returns:
        Gate name, or None when auto-update may proceed.

    """
    if not ignore_invocation:
        if os.environ.get("_HYPERCI_UPGRADING") == "1":
            return "recursion-guard"
        # `upgrade` is the deprecated alias of `update`. Both are exempt so the
        # command does not race a background update (#147).
        if len(sys.argv) >= 2 and sys.argv[1] in ("update", "upgrade", "autoupdate"):
            return "explicit-command"

    # Freeze outranks every opt-in below it, HYPERCI_AUTO_UPDATE=true included.
    if channel.is_frozen():
        return "frozen"

    # The env var beats CI detection and the persisted enable flag.
    auto_update_env = os.environ.get("HYPERCI_AUTO_UPDATE", "").lower()
    if auto_update_env == "false":
        return "env-disabled"
    if auto_update_env == "true":
        pass  # Explicit opt-in, skip CI check
    elif is_ci():
        return "ci"
    elif not channel.read_enabled():
        return "disabled"

    if _timestamp_age() < CHECK_INTERVAL:
        return "recently-checked"

    return None


def _fetch_releases(*, attempts: int = URL_ATTEMPTS) -> dict[str, list]:
    """Fetch the PyPI releases mapping, or {} on any error.

    Args:
        attempts: Tries before giving up. The check ahead of every command
            passes 1, so a machine with no route to PyPI skips the retries.

    Returns:
        PyPI releases mapping {version_string: [file_dicts]}.

    """
    try:
        req = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
        data = json.loads(url_read(req, timeout=PYPI_TIMEOUT, attempts=attempts))
    except Exception:
        return {}
    releases = data.get("releases", {})
    return releases if isinstance(releases, dict) else {}


def _run_upgrade_cmd(cmd: list[str]) -> int:
    """Run the upgrade subprocess, logging an OS-level failure as a warning.

    Returns:
        Exit code (0 = success, non-zero = failure).

    """
    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except PermissionError:
        logger.warning(
            "Permission denied -- try running with sudo or fix install permissions"
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
        os.execvpe(sys.argv[0], sys.argv, env)  # noqa: S606 -- re-execs this process's own argv, no shell involved
    except OSError:
        logger.warning("Upgrade installed but re-exec failed -- run your command again")
        raise SystemExit(0)


def _refuse_when_frozen() -> bool:
    """Report whether hyperi-ci's own freeze flag blocks an explicit upgrade.

    hyperi-ai's flag is reported and the upgrade proceeds, as the operator typed
    the command. hyperi-ci's own flag refuses.

    Returns:
        True when the caller must abort.

    """
    holders = channel.frozen_by()
    if "hyperi-ci" in holders:
        logger.error(
            "Auto-update is frozen. Clear it first: hyperi-ci autoupdate unfreeze"
        )
        return True
    if "hyperi-ai" in holders:
        logger.warning(
            "hyperi-ai auto-update is frozen on this machine; upgrading hyperi-ci "
            "anyway because it was asked for explicitly"
        )
    return False


def _explicit_target(pre: bool) -> UpgradeTarget | None:
    """Resolve the target for an unpinned ``hyperi-ci update``.

    Args:
        pre: Include pre-releases when resolving.

    Returns:
        The resolved target, or None when it could not be resolved (already
        logged).

    """
    releases = _fetch_releases()
    if not releases:
        logger.error("Could not determine latest version from PyPI")
        return None
    channel_name, source = channel.resolve_channel()
    logger.info(f"Channel: {channel_name} (from {source})")
    target = _resolve_target(releases, channel_name=channel_name, pre=pre)
    if target.note:
        logger.info(target.note)
    if target.version is None:
        logger.error(
            f"No release has soaked past the {channel.COOLDOWN_DAYS}-day cooldown "
            f"yet. Switch channel to move now: hyperi-ci autoupdate channel live"
        )
        return None
    return target


def run_upgrade(
    version: str | None = None,
    pre: bool = False,
) -> int:
    """Run an explicit upgrade.

    Args:
        version: Specific version to install, or None to follow the channel.
        pre: Include pre-releases.

    Returns:
        Exit code (0 = success).

    """
    if _refuse_when_frozen():
        return 1

    if version:
        resolved = UpgradeTarget(version=version, pin=True, note=None)
    else:
        maybe = _explicit_target(pre)
        if maybe is None:
            return 1
        resolved = maybe

    target = resolved.version
    if target is None:
        return 1
    uv_path = shutil.which("uv")
    current = _effective_current(uv_path)
    try:
        target_ver = Version(target)
    except InvalidVersion:
        logger.error(f"Invalid version: {target}")
        return 1

    if current == target_ver:
        logger.info(f"Already up to date ({current})")
        return 0

    if version is None and current > target_ver:
        # Only an explicit version may install something older.
        logger.info(f"Already ahead of the channel target ({current} > {target_ver})")
        return 0

    cmd = _build_upgrade_cmd(
        uv_path=uv_path,
        version=target if resolved.pin else None,
        pre=pre,
    )
    logger.info(f"Upgrading: {' '.join(cmd)}")

    rc = _run_upgrade_cmd(cmd)
    if rc != 0:
        logger.error(f"Upgrade failed (exit {rc})")
        return rc

    if not _confirm_upgraded(uv_path, target):
        return 1

    if version:
        # Auto-update clears the receipt pin this leaves.
        logger.warning(
            f"Installed {target} explicitly. Auto-update will move back to the "
            f"channel target within {CHECK_INTERVAL // 3600}h -- hold here with: "
            f"hyperi-ci autoupdate freeze"
        )

    logger.info(f"{PACKAGE} upgraded: {current} -> {target}")
    # No re-exec: `update` has no command to carry on with, and a binary from
    # before #82 re-execs on every "Nothing to upgrade" without end.
    return 0


def maybe_auto_update() -> None:
    """Check for updates and auto-upgrade if appropriate.

    Called from the CLI app callback. Never raises: errors are logged as
    warnings so the original command proceeds.
    """
    try:
        if not _should_auto_update():
            return

        releases = _fetch_releases(attempts=1)
        if not releases:
            return

        channel_name, _ = channel.resolve_channel()
        resolved = _resolve_target(releases, channel_name=channel_name)
        if resolved.version is None:
            # Nothing has soaked on stable yet. The check answered, so record it.
            _write_timestamp()
            return

        uv_path = shutil.which("uv")
        current = _effective_current(uv_path)
        if current >= Version(resolved.version):
            # Never downgrades: stable while ahead of the soak window holds still.
            _write_timestamp()
            return

        cmd = _build_upgrade_cmd(
            uv_path=uv_path,
            version=resolved.version if resolved.pin else None,
            pre=False,
        )

        rc = _run_upgrade_cmd(cmd)
        if rc != 0:
            logger.warning(f"Auto-update failed (exit {rc})")
            return

        if not _confirm_upgraded(uv_path, resolved.version):
            # No timestamp: a stuck install must warn again on the next run.
            return

        _write_timestamp()
        logger.info(f"{PACKAGE} upgraded: {current} -> {resolved.version}")
        _re_exec()

    except Exception as exc:
        logger.warning(f"Auto-update check failed: {exc}")


def _running_version() -> str:
    """Return the version this invocation would report as ``--version`` (#148).

    An editable install freezes its version into ``.dist-info`` at sync time, so
    ``__version__`` drifts from the tree. Deferred import because ``cli``
    imports this module.
    """
    from hyperi_ci.cli import _checkout_version, _source_checkout

    checkout = _source_checkout()
    return _checkout_version(checkout) if checkout else __version__


def autoupdate_status() -> dict:
    """Report what auto-update would do, for ``hyperi-ci autoupdate status``.

    Makes one PyPI request, and network keys come back None when PyPI cannot be
    read. ``running`` and ``installed`` differ for one invocation after an
    upgrade, and the decisions use the newer of the two.

    Returns:
        Mapping of the channel state, the resolved target, and the gates.

    """
    channel_name, source = channel.resolve_channel()
    env = os.environ.get("HYPERCI_AUTO_UPDATE", "")
    age = _timestamp_age()
    releases = _fetch_releases()
    latest, _ = _parse_latest_version(releases)
    resolved = _resolve_target(releases, channel_name=channel_name)
    return {
        "running": _running_version(),
        "installed": _installed_version(shutil.which("uv")),
        "channel": channel_name,
        "channel_source": source,
        "cooldown_days": channel.COOLDOWN_DAYS,
        "enabled": channel.read_enabled(),
        "frozen": channel.is_frozen(),
        "frozen_by": channel.frozen_by(),
        "env_override": env or None,
        "in_ci": is_ci(),
        "blocked_by": _blocking_gate(ignore_invocation=True),
        "hours_since_check": None if age == float("inf") else round(age / 3600, 2),
        "check_interval_hours": CHECK_INTERVAL // 3600,
        "latest_on_pypi": latest,
        "channel_target": resolved.version,
        "holding": resolved.note,
        "state_file": str(channel.channel_path()),
    }
