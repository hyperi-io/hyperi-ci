# Project:   HyperI CI
# File:      tests/unit/test_channel.py
# Purpose:   Tests for auto-update channel, enable flag and freeze state
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperi_ci import channel


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestReadChannel:
    """Resolve the channel from our config, hyperi-ai's, or the default."""

    def test_defaults_to_live(self) -> None:
        assert channel.resolve_channel() == ("live", "default")

    def test_reads_own_config(self) -> None:
        _write(channel.channel_path(), {"channel": "stable"})
        assert channel.resolve_channel() == ("stable", "hyperi-ci")

    def test_inherits_hyperi_ai_channel_when_we_have_none(self) -> None:
        _write(channel.AI_CONFIG_DIR / "channel.json", {"channel": "stable"})
        assert channel.resolve_channel() == ("stable", "hyperi-ai")

    def test_own_config_beats_hyperi_ai(self) -> None:
        _write(channel.channel_path(), {"channel": "live"})
        _write(channel.AI_CONFIG_DIR / "channel.json", {"channel": "stable"})
        assert channel.resolve_channel() == ("live", "hyperi-ci")

    def test_stale_hyperi_ai_nightly_normalises_to_live(self) -> None:
        """An older hyperi-ai still writes the retired name into that file."""
        _write(channel.AI_CONFIG_DIR / "channel.json", {"channel": "nightly"})
        assert channel.resolve_channel() == ("live", "hyperi-ai")

    def test_edge_alias_normalises_to_live(self) -> None:
        _write(channel.channel_path(), {"channel": "edge"})
        assert channel.read_channel() == "live"

    def test_unknown_channel_falls_back_to_default(self) -> None:
        _write(channel.channel_path(), {"channel": "banana"})
        assert channel.resolve_channel() == ("live", "default")

    def test_malformed_json_falls_back_to_default(self) -> None:
        path = channel.channel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert channel.read_channel() == "live"

    def test_non_mapping_json_falls_back_to_default(self) -> None:
        _write(channel.channel_path(), ["live"])
        assert channel.read_channel() == "live"


class TestWriteChannel:
    """Persist the canonical name, never the alias."""

    def test_round_trip(self) -> None:
        channel.write_channel("stable")
        assert channel.read_channel() == "stable"

    def test_persists_canonical_name_for_an_alias(self) -> None:
        channel.write_channel("nightly")
        stored = json.loads(channel.channel_path().read_text(encoding="utf-8"))
        assert stored["channel"] == "live"

    def test_rejects_unknown_channel(self) -> None:
        with pytest.raises(ValueError, match="Invalid channel"):
            channel.write_channel("banana")

    def test_creates_the_config_dir(self) -> None:
        assert not channel.channel_path().exists()
        channel.write_channel("live")
        assert channel.channel_path().is_file()

    def test_leaves_no_temp_file_behind(self) -> None:
        """The write renames over the target rather than truncating it."""
        channel.write_channel("stable")
        assert sorted(p.name for p in channel.CONFIG_DIR.iterdir()) == ["channel.json"]

    def test_a_half_written_file_cannot_be_read(self, monkeypatch) -> None:
        """A crash mid-write must not turn a stable machine back into live."""
        channel.write_channel("stable")
        real_replace = Path.replace

        def die_before_rename(self: Path, target: object) -> None:
            raise OSError("crashed before the rename")

        monkeypatch.setattr(Path, "replace", die_before_rename)
        with pytest.raises(OSError):
            channel.write_enabled(False)
        monkeypatch.setattr(Path, "replace", real_replace)
        assert channel.read_channel() == "stable"


class TestEnabledFlag:
    """The enable flag shares one file with the channel."""

    def test_enabled_by_default(self) -> None:
        assert channel.read_enabled() is True

    def test_disable_then_enable(self) -> None:
        channel.write_enabled(False)
        assert channel.read_enabled() is False
        channel.write_enabled(True)
        assert channel.read_enabled() is True

    def test_setting_the_channel_keeps_the_enable_flag(self) -> None:
        channel.write_enabled(False)
        channel.write_channel("stable")
        assert channel.read_enabled() is False
        assert channel.read_channel() == "stable"

    def test_setting_the_enable_flag_keeps_the_channel(self) -> None:
        channel.write_channel("stable")
        channel.write_enabled(False)
        assert channel.read_channel() == "stable"


class TestFreeze:
    """Freeze spans both tools; unfreeze only clears our own."""

    def test_not_frozen_by_default(self) -> None:
        assert channel.is_frozen() is False
        assert channel.frozen_by() == []

    def test_freeze_and_unfreeze(self) -> None:
        channel.freeze()
        assert channel.is_frozen() is True
        assert channel.frozen_by() == ["hyperi-ci"]
        channel.unfreeze()
        assert channel.is_frozen() is False

    def test_hyperi_ai_freeze_counts(self) -> None:
        ai_flag = channel.AI_CONFIG_DIR / "frozen"
        ai_flag.parent.mkdir(parents=True, exist_ok=True)
        ai_flag.touch()
        assert channel.is_frozen() is True
        assert channel.frozen_by() == ["hyperi-ai"]

    def test_unfreeze_leaves_hyperi_ai_frozen(self) -> None:
        ai_flag = channel.AI_CONFIG_DIR / "frozen"
        ai_flag.parent.mkdir(parents=True, exist_ok=True)
        ai_flag.touch()
        channel.freeze()
        channel.unfreeze()
        assert channel.freeze_path().exists() is False
        assert channel.is_frozen() is True

    def test_freeze_is_idempotent(self) -> None:
        channel.freeze()
        channel.freeze()
        assert channel.frozen_by() == ["hyperi-ci"]

    def test_unfreeze_is_idempotent(self) -> None:
        channel.unfreeze()
        channel.unfreeze()
        assert channel.is_frozen() is False

    def test_unfreeze_survives_an_unlink_error(self, monkeypatch) -> None:
        """A read-only config dir must not crash the command that clears it."""
        channel.freeze()

        def denied(self: Path, missing_ok: bool = False) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", denied)
        channel.unfreeze()
