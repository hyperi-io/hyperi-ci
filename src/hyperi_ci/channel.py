# Project:   HyperI CI
# File:      src/hyperi_ci/channel.py
# Purpose:   Auto-update channel state (live|stable), enable flag, freeze switch
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Auto-update channel, enable flag and freeze switch.

Same vocabulary as hyperi-ai, so one mental model covers both tools:

- ``live`` (DEFAULT): the newest release, adopted as soon as it exists.
- ``stable``: the newest release aged past the cooldown (7 days), trailing
  live by the soak window. For a machine that should not move under you.

The mechanism differs from hyperi-ai. hyperi-ai is a git clone, so its ``live``
follows main HEAD and its ``stable`` walks git tags. hyperi-ci is a PyPI
package with no clone, so ``live`` is the newest PUBLISHED release and
``stable`` is resolved from PyPI upload timestamps. Neither channel tracks
unreleased commits.

Upload time rather than tag date measures the soak window at the only point a
consumer can observe: when the artefact became installable.

State lives in ``~/.config/hyperi-ci/``, not hyperi-ai's directory: hyperi-ci
runs on machines with no hyperi-ai at all (CI runners, other people's boxes),
so it cannot depend on a sibling tool's private config. A machine that has
configured hyperi-ai has already stated its intent, so hyperi-ci falls back to
reading that channel when it has none of its own. The fallback is read-only,
and an explicit ``hyperi-ci autoupdate channel ...`` always wins locally.
``autoupdate status`` names which source answered.

Freeze is an orthogonal kill-switch, and it spans both tools: ``is_frozen()``
is true when either flag is set, because a freeze means nothing on the machine
should move. ``unfreeze`` clears only hyperi-ci's own flag.

hyperi-ai's two retired names for ``live`` -- ``edge`` and ``nightly`` -- are
accepted and normalised, never advertised. A hyperi-ai install lagging the
rename still writes ``nightly`` into the file this module falls back to
reading.
"""

from __future__ import annotations

import json
from pathlib import Path

VALID_CHANNELS: tuple[str, ...] = ("live", "stable")

DEFAULT_CHANNEL = "live"

# How long a release must have been on PyPI before "stable" will adopt it.
# Deliberately the same 7 days as the Actions pin cooldown in
# docs/dependencies/deps-pinning.md -- one soak window across the toolchain.
COOLDOWN_DAYS = 7

# hyperi-ai's pre-rename names for "live", oldest first. Accepted silently on
# read and write, normalised before use or persistence, never advertised.
# Listed rather than chained so a third rename does not become a third hop.
_LEGACY_CHANNEL_ALIASES: tuple[str, ...] = ("edge", "nightly")

# Module constants rather than inlined paths so tests can point them at a
# tmp_path -- no test should read the developer's real homedir config.
CONFIG_DIR = Path.home() / ".config" / "hyperi-ci"
AI_CONFIG_DIR = Path.home() / ".config" / "hyperi-ai"


def channel_path() -> Path:
    """Return hyperi-ci's channel state file path."""
    return CONFIG_DIR / "channel.json"


def freeze_path() -> Path:
    """Return hyperi-ci's freeze flag path."""
    return CONFIG_DIR / "frozen"


def _ai_channel_path() -> Path:
    """Return hyperi-ai's channel state file path (read-only fallback)."""
    return AI_CONFIG_DIR / "channel.json"


def _ai_freeze_path() -> Path:
    """Return hyperi-ai's freeze flag path (read-only)."""
    return AI_CONFIG_DIR / "frozen"


def _normalise_channel(value: object) -> str | None:
    """Map a raw channel value to its canonical name, or None if unknown.

    Args:
        value: Raw value from a config file or the CLI; any JSON type.

    Returns:
        A name from VALID_CHANNELS, or None when unrecognised so the caller
        can fall back to the default.

    """
    if isinstance(value, str) and value in VALID_CHANNELS:
        return value
    if value in _LEGACY_CHANNEL_ALIASES:
        return "live"
    return None


def _read_state(path: Path) -> dict:
    """Read a channel state file, returning {} when absent or unusable.

    Args:
        path: Channel state file to read.

    Returns:
        The parsed mapping, or an empty dict on any read/parse problem.

    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_channel() -> tuple[str, str]:
    """Return (channel, source) -- the channel and where it came from.

    Source is ``"hyperi-ci"`` (our own config), ``"hyperi-ai"`` (inherited from
    the sibling tool's config because we have none of our own), or
    ``"default"``. ``autoupdate status`` prints it so an inherited channel is
    visible rather than surprising.

    The default is ``live`` rather than ``stable`` for the same reason
    hyperi-ai chose it: a machine nobody has configured should be on the
    current code, not silently a soak window behind it.

    Returns:
        Tuple of (channel name from VALID_CHANNELS, source label).

    """
    own = _normalise_channel(_read_state(channel_path()).get("channel"))
    if own is not None:
        return own, "hyperi-ci"
    inherited = _normalise_channel(_read_state(_ai_channel_path()).get("channel"))
    if inherited is not None:
        return inherited, "hyperi-ai"
    return DEFAULT_CHANNEL, "default"


def read_channel() -> str:
    """Return the effective channel, ignoring where it came from."""
    return resolve_channel()[0]


def _write_state(**updates: object) -> None:
    """Merge updates into the channel state file, creating it if needed.

    Read-modify-write rather than overwrite: channel and the enable flag live
    in the same file, so setting one must not drop the other.

    The write goes to a temp file in the same directory and is renamed over the
    target, so a reader never sees a half-written file. An unparseable file
    reads as absent, which means the default -- and defaulting a machine from
    ``stable`` back to ``live`` is the wrong direction to fail in, so the window
    for a torn write is closed rather than tolerated.

    Args:
        **updates: Keys to set in the state mapping.

    """
    path = channel_path()
    state = _read_state(path)
    state.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(path)


def write_channel(value: str) -> None:
    """Persist the channel, normalising a legacy alias first.

    Args:
        value: Channel name, or one of hyperi-ai's retired aliases.

    Raises:
        ValueError: If ``value`` is neither a channel nor a known alias.

    """
    normalised = _normalise_channel(value)
    if normalised is None:
        raise ValueError(
            f"Invalid channel {value!r}. Must be one of: {', '.join(VALID_CHANNELS)}"
        )
    _write_state(channel=normalised)


def read_enabled() -> bool:
    """Return False only when auto-update was explicitly disabled.

    Absent or malformed state means enabled -- an unconfigured machine keeps
    tracking releases. ``HYPERCI_AUTO_UPDATE`` in the environment still wins
    over this; see ``upgrade._should_auto_update``.
    """
    return _read_state(channel_path()).get("enabled") is not False


def write_enabled(value: bool) -> None:
    """Persist the enable flag.

    Args:
        value: True to auto-update on the configured channel, False to stop.

    """
    _write_state(enabled=bool(value))


def is_frozen() -> bool:
    """Return True when either tool's freeze flag is set.

    A freeze is an incident kill-switch, so hyperi-ai's flag counts here too --
    the safe reading of a frozen machine is that nothing on it should move.
    """
    return freeze_path().exists() or _ai_freeze_path().exists()


def frozen_by() -> list[str]:
    """Return which tools' freeze flags are set, for reporting."""
    tools = []
    if freeze_path().exists():
        tools.append("hyperi-ci")
    if _ai_freeze_path().exists():
        tools.append("hyperi-ai")
    return tools


def freeze() -> None:
    """Engage hyperi-ci's auto-update freeze (idempotent)."""
    path = freeze_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def unfreeze() -> None:
    """Clear hyperi-ci's own freeze flag (idempotent).

    hyperi-ai's flag is left alone, so a machine frozen by the sibling tool
    stays frozen here; ``autoupdate unfreeze`` reports that.
    """
    try:
        freeze_path().unlink(missing_ok=True)
    except OSError:
        pass
