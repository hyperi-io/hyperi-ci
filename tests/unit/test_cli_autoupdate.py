# Project:   HyperI CI
# File:      tests/unit/test_cli_autoupdate.py
# Purpose:   Tests for the `hyperi-ci autoupdate` command surface
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""The verbs of `hyperi-ci autoupdate`, exercised through the real dispatch.

The resolution logic is covered in test_channel.py and test_upgrade.py; this
covers the wiring between them and the operator: which verb calls what, what it
prints, and the exit codes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from hyperi_ci import channel
from hyperi_ci.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the CLI callback and `status` off PyPI.

    The app callback runs maybe_auto_update on every invocation, and `status`
    resolves a real target -- both would hit the network from a unit test.
    """
    monkeypatch.setattr("hyperi_ci.upgrade.maybe_auto_update", lambda: None)
    monkeypatch.setattr("hyperi_ci.upgrade._fetch_releases", lambda: {})
    yield


class TestStatus:
    """`autoupdate` with no verb, and `autoupdate status`."""

    def test_bare_command_defaults_to_status(self) -> None:
        result = runner.invoke(app, ["autoupdate"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["channel"] == "live"

    def test_status_is_machine_readable(self) -> None:
        result = runner.invoke(app, ["autoupdate", "status"])
        payload = json.loads(result.stdout)
        for key in ("running", "installed", "channel", "channel_source", "frozen"):
            assert key in payload, key

    def test_status_reports_the_configured_channel(self) -> None:
        channel.write_channel("stable")
        payload = json.loads(runner.invoke(app, ["autoupdate", "status"]).stdout)
        assert payload["channel"] == "stable"
        assert payload["channel_source"] == "hyperi-ci"


class TestChannelVerb:
    """`autoupdate channel [live|stable]`."""

    def test_bare_channel_reports_value_and_source(self) -> None:
        result = runner.invoke(app, ["autoupdate", "channel"])
        assert result.exit_code == 0
        assert "'live'" in result.stdout
        assert "from default" in result.stdout

    def test_setting_persists(self) -> None:
        assert runner.invoke(app, ["autoupdate", "channel", "stable"]).exit_code == 0
        assert channel.read_channel() == "stable"

    def test_it_echoes_the_canonical_name_for_an_alias(self) -> None:
        """A retired alias is accepted; the echo must not repeat it back."""
        result = runner.invoke(app, ["autoupdate", "channel", "nightly"])
        assert result.exit_code == 0
        assert "'live'" in result.stdout
        assert "nightly" not in result.stdout

    def test_an_unknown_channel_exits_one(self) -> None:
        result = runner.invoke(app, ["autoupdate", "channel", "banana"])
        assert result.exit_code == 1
        assert "Invalid channel" in result.output

    def test_a_rejected_value_is_not_persisted(self) -> None:
        channel.write_channel("stable")
        runner.invoke(app, ["autoupdate", "channel", "banana"])
        assert channel.read_channel() == "stable"


class TestEnableDisable:
    """`autoupdate enable` / `disable`."""

    def test_disable_persists(self) -> None:
        assert runner.invoke(app, ["autoupdate", "disable"]).exit_code == 0
        assert channel.read_enabled() is False

    def test_enable_persists(self) -> None:
        channel.write_enabled(False)
        assert runner.invoke(app, ["autoupdate", "enable"]).exit_code == 0
        assert channel.read_enabled() is True

    def test_it_keeps_the_channel(self) -> None:
        channel.write_channel("stable")
        runner.invoke(app, ["autoupdate", "disable"])
        assert channel.read_channel() == "stable"

    def test_it_warns_when_the_env_var_overrides_the_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stored flag that the environment overrides must not read as applied."""
        monkeypatch.setenv("HYPERCI_AUTO_UPDATE", "true")
        result = runner.invoke(app, ["autoupdate", "disable"])
        assert "HYPERCI_AUTO_UPDATE" in result.stdout

    def test_no_warning_when_the_env_var_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HYPERCI_AUTO_UPDATE", raising=False)
        result = runner.invoke(app, ["autoupdate", "disable"])
        assert "HYPERCI_AUTO_UPDATE" not in result.stdout


class TestFreeze:
    """`autoupdate freeze` / `unfreeze`, including the sibling tool's flag."""

    def test_freeze_engages(self) -> None:
        assert runner.invoke(app, ["autoupdate", "freeze"]).exit_code == 0
        assert channel.is_frozen() is True

    def test_freeze_names_the_way_out(self) -> None:
        result = runner.invoke(app, ["autoupdate", "freeze"])
        assert "unfreeze" in result.stdout

    def test_unfreeze_clears(self) -> None:
        channel.freeze()
        assert runner.invoke(app, ["autoupdate", "unfreeze"]).exit_code == 0
        assert channel.is_frozen() is False

    def test_unfreeze_says_when_hyperi_ai_still_holds_it(self) -> None:
        """Clearing our flag while the sibling's is set must not read as unfrozen."""
        ai_flag = channel.AI_CONFIG_DIR / "frozen"
        ai_flag.parent.mkdir(parents=True, exist_ok=True)
        ai_flag.touch()
        channel.freeze()
        result = runner.invoke(app, ["autoupdate", "unfreeze"])
        assert result.exit_code == 0
        assert "hyperi-ai" in result.stdout
        assert channel.is_frozen() is True

    def test_unfreeze_is_quiet_when_nothing_else_holds_it(self) -> None:
        channel.freeze()
        result = runner.invoke(app, ["autoupdate", "unfreeze"])
        assert "hyperi-ai" not in result.stdout


class TestUnknownAction:
    """An unrecognised verb fails loudly rather than doing nothing."""

    def test_it_exits_one(self) -> None:
        assert runner.invoke(app, ["autoupdate", "bogus"]).exit_code == 1

    def test_it_lists_the_valid_verbs(self) -> None:
        output = runner.invoke(app, ["autoupdate", "bogus"]).output
        for verb in ("status", "enable", "disable", "channel", "freeze", "unfreeze"):
            assert verb in output, verb

    def test_it_changes_no_state(self) -> None:
        channel.write_channel("stable")
        runner.invoke(app, ["autoupdate", "bogus"])
        assert channel.read_channel() == "stable"
        assert channel.read_enabled() is True
