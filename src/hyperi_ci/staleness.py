# Project:   HyperI CI
# File:      src/hyperi_ci/staleness.py
# Purpose:   Warn when the running CLI is not the latest published release
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Say so when this hyperi-ci is not the current one.

An ephemeral install -- ``uvx hyperi-ci``, ``uv run --with hyperi-ci`` -- takes
the project's interpreter and back-solves to the newest release that
interpreter allows. Where the latest release declares a floor above it, the
resolution lands on an older hyperi-ci and reports nothing: issue #162 was a
careful bug report written against code released the day before, and the
reporter had no way to see that.

Two facts make the warning worth reading. The running and latest versions come
from PyPI, and so does the latest release's own ``requires-python``, so the
remedy names the interpreter that actually resolves the current release rather
than a number written down here that rots at the next floor move.

The check informs and never acts: silent in CI, silent on a source checkout,
silent when PyPI cannot be reached, and at most one warning a day. Acting on a
stale install is :mod:`hyperi_ci.upgrade`'s job, and an install whose
auto-update is working is never stale enough to trip this.
"""

import sys
import time

from packaging.version import InvalidVersion, Version

from hyperi_ci import __version__, channel
from hyperi_ci.common import env_true, is_ci, warn
from hyperi_ci.python_version import floor_from_specifier
from hyperi_ci.upgrade import CACHE_DIR, _fetch_releases, _parse_latest_version

CHECK_INTERVAL = 24 * 60 * 60
TIMESTAMP_FILE = CACHE_DIR / "last-stale-check"
SILENCE_ENV = "HYPERCI_NO_STALE_WARNING"


def _latest_requires_python(releases: dict[str, list], version: str) -> str | None:
    """Return the ``requires-python`` specifier one release publishes.

    Args:
        releases: PyPI releases mapping {version_string: [file_dicts]}.
        version: Release to read.

    Returns:
        The specifier string carried by the release's files, or None when the
        release is absent or names none.

    """
    for entry in releases.get(version, []):
        if not isinstance(entry, dict):
            continue
        spec = entry.get("requires_python")
        if isinstance(spec, str) and spec.strip():
            return spec
    return None


def _interpreter_below(floor: str) -> bool:
    """Return whether the running interpreter is older than ``floor``.

    Args:
        floor: A ``major.minor`` version.

    Returns:
        True when this interpreter cannot satisfy the floor.

    """
    try:
        major, minor = (int(part) for part in floor.split(".")[:2])
    except ValueError:
        return False
    return (major, minor) > sys.version_info[:2]


def staleness_lines(
    running: str,
    latest: str,
    min_python: str | None,
) -> list[str]:
    """Build the warning for a build that is behind the latest release.

    Args:
        running: Version of the build executing this call.
        latest: Newest version published to PyPI.
        min_python: Lowest Python the latest release accepts, or None when it
            publishes no floor.

    Returns:
        The lines to emit, empty when ``running`` is not behind ``latest``.

    """
    try:
        if Version(running) >= Version(latest):
            return []
    except InvalidVersion:
        return []

    pin = f"--python {min_python} " if min_python else ""
    if min_python is not None and _interpreter_below(min_python):
        here = f"{sys.version_info.major}.{sys.version_info.minor}"
        headline = (
            f"hyperi-ci {running} is behind the latest release {latest}, and "
            f"Python {here} cannot resolve it -- {latest} needs Python "
            f">= {min_python}."
        )
    else:
        headline = (
            f"hyperi-ci {running} is behind the latest release {latest} -- a "
            f"bug you hit here may already be fixed."
        )
    return [
        headline,
        f"Run the current one from any project: uvx {pin}hyperi-ci <command>",
        "Or move the installed tool: hyperi-ci update",
    ]


def lines_for_releases(releases: dict[str, list], running: str) -> list[str]:
    """Decide what to say about ``running`` given a PyPI releases mapping.

    The whole decision sits here so it can be exercised against a captured
    payload rather than against the network.

    Args:
        releases: PyPI releases mapping {version_string: [file_dicts]}.
        running: Version of the build executing this call.

    Returns:
        The lines to emit, empty when there is nothing to report.

    """
    latest, _ = _parse_latest_version(releases)
    if latest is None:
        return []
    spec = _latest_requires_python(releases, latest)
    return staleness_lines(
        running,
        latest,
        floor_from_specifier(spec) if spec else None,
    )


def _check_is_due(now: float) -> bool:
    """Return whether the daily check has come round again.

    Args:
        now: Unix timestamp to age the last check against.

    Returns:
        True when no readable check is on record, or the last one has aged out.

    """
    try:
        last = float(TIMESTAMP_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return (now - last) >= CHECK_INTERVAL


def _record_check(now: float) -> None:
    """Record that the check ran, whatever it concluded.

    Args:
        now: Unix timestamp to write.

    """
    TIMESTAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    TIMESTAMP_FILE.write_text(str(now), encoding="utf-8", newline="\n")


def _suppressed() -> bool:
    """Return whether something has already answered the staleness question.

    A frozen auto-update is the operator holding the install at a version
    deliberately, which is a decision rather than a stale install. A source
    checkout is being worked on, and its tree is behind PyPI by design.

    Returns:
        True when the warning must stay quiet.

    """
    if is_ci() or env_true(SILENCE_ENV) or channel.is_frozen():
        return True
    from hyperi_ci.cli import _source_checkout

    return _source_checkout() is not None


def _check(now: float) -> list[str]:
    """Run the check and emit the warning.

    The check is recorded before PyPI is asked, so a machine with no route to
    PyPI waits out the interval rather than paying the connection timeout on
    every command.

    Args:
        now: Unix timestamp for the freshness window.

    Returns:
        The lines emitted, empty when nothing was said.

    """
    if _suppressed() or not _check_is_due(now):
        return []
    _record_check(now)

    lines = lines_for_releases(_fetch_releases(), __version__)
    for line in lines:
        warn(line)
    return lines


def warn_if_stale(*, now: float | None = None) -> list[str]:
    """Warn once a day when this build is not the latest published release.

    Called from the CLI entry point. Every failure path is silence: a warning
    that can break a build is not a warning.

    Args:
        now: Unix timestamp to age the last check against (tests); defaults to
            the clock.

    Returns:
        The lines emitted, empty when nothing was said.

    """
    try:
        return _check(time.time() if now is None else now)
    except Exception:  # noqa: BLE001 - informing the operator never fails a run
        return []
