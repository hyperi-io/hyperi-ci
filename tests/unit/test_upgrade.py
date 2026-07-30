# Project:   HyperI CI
# File:      tests/unit/test_upgrade.py
# Purpose:   Tests for self-upgrade functionality
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from packaging.version import Version

from hyperi_ci import channel
from hyperi_ci.upgrade import (
    CHECK_INTERVAL,
    _blocking_gate,
    _build_upgrade_cmd,
    _confirm_upgraded,
    _effective_current,
    _fetch_pypi_versions,
    _installed_version,
    _newest_with_age,
    _parse_installed_version,
    _parse_latest_version,
    _release_upload_time,
    _resolve_target,
    _run_upgrade_cmd,
    _should_auto_update,
    _soaked_version,
    autoupdate_status,
    maybe_auto_update,
    run_upgrade,
)

# Ages are measured from the module-load clock, so the resolvers called with an
# explicit `now` and the paths that read the real clock agree on them.
NOW = time.time()
DAY = 86400.0


def _iso(offset_days: float) -> str:
    """Return an ISO upload time that many days before NOW."""
    from datetime import UTC, datetime

    return (
        datetime.fromtimestamp(NOW - offset_days * DAY, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _releases(**ages: float) -> dict[str, list]:
    """Build a PyPI releases mapping from {version: age in days}."""
    return {ver: [{"upload_time_iso_8601": _iso(age)}] for ver, age in ages.items()}


@pytest.fixture(autouse=True)
def _no_installer_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the suite reading whichever hyperi-ci this machine has installed.

    `_effective_current` asks the installer for the on-disk version, so without
    this a developer with 2.9.6 installed sees different results from CI. Tests
    that exercise the lookup patch it themselves or call it directly.
    """
    monkeypatch.setattr("hyperi_ci.upgrade._installed_version", lambda _uv: None)


# 2.9.6 is inside the 7-day window, so stable holds at 2.9.4 while live takes it.
SOAK_SAMPLE = {
    "2.9.4": [{"upload_time_iso_8601": _iso(30)}],
    "2.9.5": [{"upload_time_iso_8601": _iso(2)}],
    "2.9.6": [{"upload_time_iso_8601": _iso(1)}],
    "3.0.0rc1": [{"upload_time_iso_8601": _iso(40)}],
}


class TestParseLatestVersion:
    """Parse latest stable and pre-release versions from PyPI JSON."""

    SAMPLE_RELEASES = {
        "1.0.0": [{}],
        "1.1.0": [{}],
        "1.1.23": [{}],
        "1.2.0": [{}],
        "1.3.0rc1": [{}],
        "1.3.0.dev4": [{}],
    }

    def test_latest_stable(self) -> None:
        stable, _ = _parse_latest_version(self.SAMPLE_RELEASES)
        assert stable == "1.2.0"

    def test_latest_prerelease(self) -> None:
        _, pre = _parse_latest_version(self.SAMPLE_RELEASES)
        assert pre == "1.3.0rc1"

    def test_no_stable_releases(self) -> None:
        releases = {"1.0.0rc1": [{}], "1.0.0.dev1": [{}]}
        stable, pre = _parse_latest_version(releases)
        assert stable is None
        assert pre == "1.0.0rc1"

    def test_empty_releases(self) -> None:
        stable, pre = _parse_latest_version({})
        assert stable is None
        assert pre is None

    def test_ignores_releases_with_no_files(self) -> None:
        releases = {"1.0.0": [{}], "1.1.0": []}
        stable, _ = _parse_latest_version(releases)
        assert stable == "1.0.0"


class TestBuildUpgradeCmd:
    """Build the correct upgrade command based on install method."""

    def test_uv_latest_uses_at_latest_not_tool_upgrade(self) -> None:
        """`tool upgrade` no-ops on a pinned receipt; `@latest` clears the pin."""
        cmd = _build_upgrade_cmd(uv_path="/usr/bin/uv", version=None, pre=False)
        assert cmd == [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            "--refresh",
            "hyperi-ci@latest",
        ]
        assert "upgrade" not in cmd

    def test_uv_pinned(self) -> None:
        cmd = _build_upgrade_cmd(uv_path="/usr/bin/uv", version="1.2.0", pre=False)
        assert cmd == [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            "--refresh",
            "hyperi-ci==1.2.0",
        ]

    def test_uv_pre(self) -> None:
        cmd = _build_upgrade_cmd(uv_path="/usr/bin/uv", version=None, pre=True)
        assert cmd == [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            "--refresh",
            "--prerelease=allow",
            "hyperi-ci@latest",
        ]

    def test_every_uv_path_refreshes_the_index(self) -> None:
        """@latest resolves against uv's cached index, so it can miss a release."""
        for version in (None, "1.2.0"):
            for pre in (False, True):
                cmd = _build_upgrade_cmd(
                    uv_path="/usr/bin/uv", version=version, pre=pre
                )
                assert "--refresh" in cmd, f"missing for version={version} pre={pre}"

    def test_pip_latest(self) -> None:
        cmd = _build_upgrade_cmd(uv_path=None, version=None, pre=False)
        assert cmd == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "hyperi-ci",
        ]

    def test_pip_pinned(self) -> None:
        cmd = _build_upgrade_cmd(uv_path=None, version="1.2.0", pre=False)
        assert cmd == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "hyperi-ci==1.2.0",
        ]

    def test_pip_pre(self) -> None:
        cmd = _build_upgrade_cmd(uv_path=None, version=None, pre=True)
        assert cmd == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--pre",
            "hyperi-ci",
        ]


class TestParseInstalledVersion:
    """Read the on-disk version out of real uv and pip output."""

    # Verbatim `uv tool list` from a dev box. The entrypoint lines matter: every
    # tool repeats its own name indented under itself, so a naive
    # startswith("hyperi-ci") match hits "- hyperi-ci" and finds no version.
    UV_TOOL_LIST = """ansible-core v2.21.2
- ansible
- ansible-config
- ansible-playbook
ansible-lint v26.6.0
- ansible-lint
headroom-ai v0.32.1
- headroom
hyperi-ai v3.16.96.dev1+g0e7629869
- hyperi-ai
hyperi-ci v2.9.5
- hyperi-ci
semgrep v1.171.0
- pysemgrep
- semgrep
"""

    PIP_SHOW = """Name: hyperi-ci
Version: 2.9.5
Summary: HyperI CI orchestrator
Location: /Users/x/.venv/lib/python3.12/site-packages
Requires: packaging, scalo, typer
"""

    def test_uv_tool_list(self) -> None:
        assert _parse_installed_version(self.UV_TOOL_LIST, from_uv=True) == "2.9.5"

    def test_uv_entrypoint_line_is_not_mistaken_for_the_tool(self) -> None:
        """An entrypoint line repeats the name but carries no version."""
        only_entrypoint = "somethingelse v1.0.0\n- hyperi-ci\n"
        assert _parse_installed_version(only_entrypoint, from_uv=True) is None

    def test_uv_not_installed(self) -> None:
        assert _parse_installed_version("ruff v0.6.0\n- ruff\n", from_uv=True) is None

    def test_uv_prerelease_and_local_version(self) -> None:
        out = "hyperi-ci v3.0.0rc1+g0e76298\n- hyperi-ci\n"
        assert _parse_installed_version(out, from_uv=True) == "3.0.0rc1+g0e76298"

    def test_pip_show(self) -> None:
        assert _parse_installed_version(self.PIP_SHOW, from_uv=False) == "2.9.5"

    def test_pip_show_empty(self) -> None:
        assert _parse_installed_version("", from_uv=False) is None

    def test_uv_parser_does_not_accept_pip_output(self) -> None:
        """Wrong from_uv flag must return None, not a wrong answer."""
        assert _parse_installed_version(self.PIP_SHOW, from_uv=True) is None


class TestConfirmUpgraded:
    """Exit code 0 is not evidence; the installed version is."""

    def test_true_when_version_moved(self) -> None:
        with patch("hyperi_ci.upgrade._installed_version", return_value="2.9.5"):
            assert _confirm_upgraded("/usr/bin/uv", "2.9.5") is True

    def test_true_when_installed_is_newer_than_target(self) -> None:
        with patch("hyperi_ci.upgrade._installed_version", return_value="2.10.0"):
            assert _confirm_upgraded("/usr/bin/uv", "2.9.5") is True

    def test_false_when_pinned_install_did_not_move(self) -> None:
        """The reported bug: uv says "Nothing to upgrade" and exits 0."""
        with patch("hyperi_ci.upgrade._installed_version", return_value="2.9.3"):
            assert _confirm_upgraded("/usr/bin/uv", "2.9.4") is False

    def test_false_when_version_unreadable(self) -> None:
        with patch("hyperi_ci.upgrade._installed_version", return_value=None):
            assert _confirm_upgraded("/usr/bin/uv", "2.9.4") is False

    def test_false_on_unparseable_installed_version(self) -> None:
        with patch(
            "hyperi_ci.upgrade._installed_version", return_value="not-a-version"
        ):
            assert _confirm_upgraded("/usr/bin/uv", "2.9.4") is False


class TestInstalledVersionAgainstRealUv:
    """Run the real installer, no mocks -- skip when it is not there."""

    def test_reads_a_version_from_real_uv(self) -> None:
        uv_path = shutil.which("uv")
        if uv_path is None:
            pytest.skip("uv not on PATH")
        version = _installed_version(uv_path)
        if version is None:
            pytest.skip("hyperi-ci is not installed as a uv tool on this machine")
        # Whatever it is, it has to be a version we can compare against.
        assert Version(version) >= Version("0")

    def test_returns_none_when_installer_is_missing(self) -> None:
        assert _installed_version("/nonexistent/bin/uv") is None


class TestShouldAutoUpdate:
    """Gate checks for auto-update."""

    def test_disabled_by_env(self) -> None:
        with patch.dict(os.environ, {"HYPERCI_AUTO_UPDATE": "false"}):
            assert _should_auto_update() is False

    def test_disabled_in_ci(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("HYPERCI_AUTO_UPDATE",)}
        env["CI"] = "true"
        with patch.dict(os.environ, env, clear=True):
            assert _should_auto_update() is False

    def test_enabled_in_ci_with_explicit_opt_in(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("_HYPERCI_UPGRADING",)}
        env.update({"CI": "true", "HYPERCI_AUTO_UPDATE": "true"})
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "hyperi_ci.upgrade._timestamp_age",
                return_value=CHECK_INTERVAL + 1,
            ):
                assert _should_auto_update() is True

    def test_disabled_by_recursion_guard(self) -> None:
        with patch.dict(os.environ, {"_HYPERCI_UPGRADING": "1"}):
            assert _should_auto_update() is False

    def test_skipped_when_recently_checked(self, tmp_path: Path) -> None:
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "ts"):
            ts_file = tmp_path / "ts"
            ts_file.write_text(str(time.time()))
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "CI",
                    "GITHUB_ACTIONS",
                    "GITLAB_CI",
                    "JENKINS_URL",
                    "BUILDKITE",
                    "_HYPERCI_UPGRADING",
                    "HYPERCI_AUTO_UPDATE",
                )
            }
            with patch.dict(os.environ, env, clear=True):
                assert _should_auto_update() is False

    def test_allowed_when_check_is_stale(self, tmp_path: Path) -> None:
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "ts"):
            ts_file = tmp_path / "ts"
            ts_file.write_text(str(time.time() - CHECK_INTERVAL - 1))
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "CI",
                    "GITHUB_ACTIONS",
                    "GITLAB_CI",
                    "JENKINS_URL",
                    "BUILDKITE",
                    "_HYPERCI_UPGRADING",
                    "HYPERCI_AUTO_UPDATE",
                )
            }
            with patch.dict(os.environ, env, clear=True):
                assert _should_auto_update() is True

    def test_allowed_when_no_timestamp(self, tmp_path: Path) -> None:
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "nonexistent"):
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "CI",
                    "GITHUB_ACTIONS",
                    "GITLAB_CI",
                    "JENKINS_URL",
                    "BUILDKITE",
                    "_HYPERCI_UPGRADING",
                    "HYPERCI_AUTO_UPDATE",
                )
            }
            with patch.dict(os.environ, env, clear=True):
                assert _should_auto_update() is True

    def test_skipped_when_command_is_upgrade(self) -> None:
        with patch("sys.argv", ["hyperi-ci", "upgrade"]):
            assert _should_auto_update() is False


class TestFetchPypiVersions:
    """Fetch and parse versions from PyPI (with mocked network)."""

    def test_parses_response(self) -> None:
        sample = json.dumps(
            {
                "releases": {
                    "1.0.0": [{"filename": "x"}],
                    "1.1.0": [{"filename": "x"}],
                    "1.2.0rc1": [{"filename": "x"}],
                }
            }
        ).encode()

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = lambda s, *a: None
            mock_urlopen.return_value.read.return_value = sample
            stable, pre = _fetch_pypi_versions()

        assert stable == "1.1.0"
        assert pre == "1.2.0rc1"

    def test_returns_none_on_network_error(self) -> None:
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            stable, pre = _fetch_pypi_versions()
        assert stable is None
        assert pre is None


class TestReleaseUploadTime:
    """When a release first became installable."""

    def test_earliest_file_wins(self) -> None:
        files = [
            {"upload_time_iso_8601": _iso(1)},
            {"upload_time_iso_8601": _iso(5)},
        ]
        assert _release_upload_time(files) == pytest.approx(NOW - 5 * DAY)

    def test_falls_back_to_upload_time(self) -> None:
        """The non-8601 key is the older PyPI field name for the same value."""
        files = [{"upload_time": "2026-07-29T22:38:10"}]
        assert _release_upload_time(files) is not None

    def test_an_offsetless_time_is_read_as_utc_not_local(self) -> None:
        """Reading PyPI's naive `upload_time` as local time skews the soak."""
        naive = _release_upload_time([{"upload_time": "2026-07-29T22:38:10"}])
        aware = _release_upload_time([{"upload_time_iso_8601": "2026-07-29T22:38:10Z"}])
        assert naive == aware

    def test_unparseable_time_is_ignored(self) -> None:
        files = [{"upload_time_iso_8601": "not-a-time"}]
        assert _release_upload_time(files) is None

    def test_no_time_at_all(self) -> None:
        assert _release_upload_time([{"filename": "x"}]) is None

    def test_empty(self) -> None:
        assert _release_upload_time([]) is None


class TestSoakedVersion:
    """Which release the stable channel is allowed to adopt."""

    def test_picks_the_newest_release_past_the_cooldown(self) -> None:
        assert _soaked_version(SOAK_SAMPLE, now=NOW) == "2.9.4"

    def test_adopts_a_release_once_it_ages_in(self) -> None:
        # 6.5 rather than 6 days on: it puts 2.9.6 at 7.5 days, clear of the
        # boundary. At exactly 7.0 the microsecond rounding of the ISO
        # round-trip decides the answer, which is what the two tests below pin
        # deliberately instead of by accident.
        later = NOW + 6.5 * DAY
        assert _soaked_version(SOAK_SAMPLE, now=later) == "2.9.6"

    def test_exactly_at_the_cooldown_qualifies(self) -> None:
        """The window is >= cooldown, so the boundary itself is in."""
        files = [{"upload_time_iso_8601": _iso(0)}]
        uploaded = _release_upload_time(files)
        assert uploaded is not None
        releases = {"1.0.0": files}
        assert _soaked_version(releases, now=uploaded + 7 * DAY) == "1.0.0"

    def test_a_moment_short_of_the_cooldown_does_not(self) -> None:
        files = [{"upload_time_iso_8601": _iso(0)}]
        uploaded = _release_upload_time(files)
        assert uploaded is not None
        releases = {"1.0.0": files}
        assert _soaked_version(releases, now=uploaded + 7 * DAY - 1) is None

    def test_none_when_nothing_has_soaked(self) -> None:
        assert _soaked_version(_releases(**{"1.0.0": 1.0}), now=NOW) is None

    def test_ignores_prereleases(self) -> None:
        """3.0.0rc1 is the oldest here, and still must not be adopted."""
        assert _soaked_version(SOAK_SAMPLE, now=NOW) != "3.0.0rc1"

    def test_undatable_release_is_skipped_not_assumed_old(self) -> None:
        releases = {
            "2.0.0": [{"filename": "x"}],
            "1.0.0": [{"upload_time_iso_8601": _iso(30)}],
        }
        assert _soaked_version(releases, now=NOW) == "1.0.0"

    def test_empty_releases(self) -> None:
        assert _soaked_version({}, now=NOW) is None

    def test_release_with_no_files_is_skipped(self) -> None:
        releases = {"2.0.0": [], "1.0.0": [{"upload_time_iso_8601": _iso(30)}]}
        assert _soaked_version(releases, now=NOW) == "1.0.0"


class TestNewestWithAge:
    """What stable is waiting on, for reporting."""

    def test_reports_the_newest_release_and_its_age(self) -> None:
        result = _newest_with_age(SOAK_SAMPLE, now=NOW)
        assert result is not None
        version, age = result
        assert version == "2.9.6"
        assert age == pytest.approx(1.0)

    def test_none_when_nothing_is_datable(self) -> None:
        assert _newest_with_age({"1.0.0": [{"filename": "x"}]}, now=NOW) is None


class TestResolveTarget:
    """The channel decides which release an upgrade aims at."""

    def test_live_takes_the_newest_release(self) -> None:
        target = _resolve_target(SOAK_SAMPLE, channel_name="live", now=NOW)
        assert target.version == "2.9.6"
        assert target.pin is False
        assert target.note is None

    def test_stable_holds_at_the_soaked_release(self) -> None:
        target = _resolve_target(SOAK_SAMPLE, channel_name="stable", now=NOW)
        assert target.version == "2.9.4"

    def test_stable_pins_while_it_lags(self) -> None:
        """@latest would install the edge, so the lagging target must be exact."""
        target = _resolve_target(SOAK_SAMPLE, channel_name="stable", now=NOW)
        assert target.pin is True

    def test_stable_does_not_pin_once_caught_up(self) -> None:
        """No receipt pin in the steady state, so `uv tool upgrade` still works."""
        caught_up = _releases(**{"2.9.4": 30.0, "2.9.6": 10.0})
        target = _resolve_target(caught_up, channel_name="stable", now=NOW)
        assert target.version == "2.9.6"
        assert target.pin is False

    def test_stable_says_what_it_is_holding_out_on(self) -> None:
        target = _resolve_target(SOAK_SAMPLE, channel_name="stable", now=NOW)
        assert target.note is not None
        assert "2.9.6" in target.note
        assert "2.9.4" in target.note

    def test_stable_with_nothing_soaked_yet(self) -> None:
        target = _resolve_target(
            _releases(**{"1.0.0": 1.0}), channel_name="stable", now=NOW
        )
        assert target.version is None
        assert target.note is not None

    def test_pre_resolves_as_live_whatever_the_channel(self) -> None:
        target = _resolve_target(SOAK_SAMPLE, channel_name="stable", pre=True, now=NOW)
        assert target.version == "3.0.0rc1"
        assert target.pin is False

    def test_unknown_channel_name_behaves_as_live(self) -> None:
        target = _resolve_target(SOAK_SAMPLE, channel_name="banana", now=NOW)
        assert target.version == "2.9.6"


class TestBlockingGate:
    """Which gate holds auto-update back, in precedence order."""

    def _clean_env(self) -> dict[str, str]:
        drop = (
            "CI",
            "GITHUB_ACTIONS",
            "GITLAB_CI",
            "JENKINS_URL",
            "BUILDKITE",
            "_HYPERCI_UPGRADING",
            "HYPERCI_AUTO_UPDATE",
        )
        return {k: v for k, v in os.environ.items() if k not in drop}

    def test_no_gate_when_nothing_blocks(self, tmp_path: Path) -> None:
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
            with patch.dict(os.environ, self._clean_env(), clear=True):
                assert _blocking_gate() is None

    def test_freeze_beats_an_explicit_env_opt_in(self, tmp_path: Path) -> None:
        """A kill-switch that an opt-in overrides is not a kill-switch."""
        channel.freeze()
        env = self._clean_env()
        env["HYPERCI_AUTO_UPDATE"] = "true"
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
            with patch.dict(os.environ, env, clear=True):
                assert _blocking_gate() == "frozen"

    def test_hyperi_ai_freeze_blocks_too(self, tmp_path: Path) -> None:
        ai_flag = channel.AI_CONFIG_DIR / "frozen"
        ai_flag.parent.mkdir(parents=True, exist_ok=True)
        ai_flag.touch()
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
            with patch.dict(os.environ, self._clean_env(), clear=True):
                assert _blocking_gate() == "frozen"

    def test_disabled_flag_blocks(self, tmp_path: Path) -> None:
        channel.write_enabled(False)
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
            with patch.dict(os.environ, self._clean_env(), clear=True):
                assert _blocking_gate() == "disabled"

    def test_env_opt_in_beats_the_disabled_flag(self, tmp_path: Path) -> None:
        channel.write_enabled(False)
        env = self._clean_env()
        env["HYPERCI_AUTO_UPDATE"] = "true"
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
            with patch.dict(os.environ, env, clear=True):
                assert _blocking_gate() is None

    def test_env_disabled_is_named(self) -> None:
        with patch.dict(os.environ, {"HYPERCI_AUTO_UPDATE": "false"}):
            assert _blocking_gate() == "env-disabled"

    def test_ci_is_named(self) -> None:
        env = self._clean_env()
        env["CI"] = "true"
        with patch.dict(os.environ, env, clear=True):
            assert _blocking_gate() == "ci"

    def test_explicit_command_is_named(self) -> None:
        with patch("sys.argv", ["hyperi-ci", "autoupdate", "status"]):
            assert _blocking_gate() == "explicit-command"

    def test_ignore_invocation_skips_the_command_gates(self, tmp_path: Path) -> None:
        """Status must report the machine's config, not that status was typed."""
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
            with patch.dict(os.environ, self._clean_env(), clear=True):
                with patch("sys.argv", ["hyperi-ci", "autoupdate"]):
                    assert _blocking_gate(ignore_invocation=True) is None

    def test_managing_autoupdate_does_not_trigger_an_update(self) -> None:
        with patch("sys.argv", ["hyperi-ci", "autoupdate", "channel", "stable"]):
            assert _should_auto_update() is False


class TestAutoupdateStatus:
    """What `hyperi-ci autoupdate status` reports."""

    def test_reports_channel_source_and_target(self, tmp_path: Path) -> None:
        channel.write_channel("stable")
        with patch("hyperi_ci.upgrade._fetch_releases", return_value=SOAK_SAMPLE):
            with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
                status = autoupdate_status()
        assert status["channel_target"] == "2.9.4"
        assert status["holding"] is not None
        assert status["channel"] == "stable"
        assert status["channel_source"] == "hyperi-ci"
        assert status["latest_on_pypi"] == "2.9.6"
        assert status["enabled"] is True
        assert status["frozen"] is False
        assert status["hours_since_check"] is None

    def test_survives_pypi_being_unreachable(self, tmp_path: Path) -> None:
        with patch("hyperi_ci.upgrade._fetch_releases", return_value={}):
            with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
                status = autoupdate_status()
        assert status["latest_on_pypi"] is None
        assert status["channel_target"] is None

    def test_separates_the_running_version_from_the_installed_one(
        self, tmp_path: Path
    ) -> None:
        """Conflating the two is what #82 was: a self-upgrade cannot see itself."""
        with patch("hyperi_ci.upgrade._fetch_releases", return_value={}):
            with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
                with patch("hyperi_ci.upgrade.__version__", "2.9.3"):
                    with patch(
                        "hyperi_ci.upgrade._installed_version", return_value="2.9.6"
                    ):
                        status = autoupdate_status()
        assert status["running"] == "2.9.3"
        assert status["installed"] == "2.9.6"

    def test_names_the_freeze_holder(self, tmp_path: Path) -> None:
        channel.freeze()
        with patch("hyperi_ci.upgrade._fetch_releases", return_value={}):
            with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "none"):
                status = autoupdate_status()
        assert status["frozen"] is True
        assert status["frozen_by"] == ["hyperi-ci"]
        assert status["blocked_by"] == "frozen"


class TestEffectiveCurrent:
    """The running version and the one on disk are not the same number."""

    def test_uses_the_running_version_when_disk_is_unreadable(self) -> None:
        with patch("hyperi_ci.upgrade.__version__", "2.9.3"):
            assert _effective_current("/uv") == Version("2.9.3")

    def test_takes_the_installed_version_when_it_is_newer(self) -> None:
        """A source checkout runs an old version against a current install."""
        with patch("hyperi_ci.upgrade.__version__", "2.3.10"):
            with patch("hyperi_ci.upgrade._installed_version", return_value="2.9.6"):
                assert _effective_current("/uv") == Version("2.9.6")

    def test_keeps_the_running_version_when_it_is_newer(self) -> None:
        with patch("hyperi_ci.upgrade.__version__", "2.9.6"):
            with patch("hyperi_ci.upgrade._installed_version", return_value="2.9.2"):
                assert _effective_current("/uv") == Version("2.9.6")

    def test_unparseable_installed_version_is_ignored(self) -> None:
        with patch("hyperi_ci.upgrade.__version__", "2.9.3"):
            with patch(
                "hyperi_ci.upgrade._installed_version", return_value="not-a-version"
            ):
                assert _effective_current("/uv") == Version("2.9.3")

    def test_stable_does_not_downgrade_a_newer_install(self, tmp_path: Path) -> None:
        """Running from a checkout must not drag the install back to the soak."""
        channel.write_channel("stable")
        with patch("hyperi_ci.upgrade._fetch_releases", return_value=SOAK_SAMPLE):
            with patch("hyperi_ci.upgrade.__version__", "2.3.10"):
                with patch(
                    "hyperi_ci.upgrade._installed_version", return_value="2.9.6"
                ):
                    with patch("hyperi_ci.upgrade._run_upgrade_cmd") as mock_run:
                        assert run_upgrade() == 0
        mock_run.assert_not_called()


class TestRunUpgradeFreeze:
    """An explicit upgrade against the freeze switch."""

    def test_refused_when_hyperi_ci_is_frozen(self) -> None:
        channel.freeze()
        with patch("hyperi_ci.upgrade._run_upgrade_cmd") as mock_run:
            assert run_upgrade() == 1
        mock_run.assert_not_called()

    def test_proceeds_when_only_hyperi_ai_is_frozen(self) -> None:
        """A sibling tool's flag is not a veto on a command typed here."""
        ai_flag = channel.AI_CONFIG_DIR / "frozen"
        ai_flag.parent.mkdir(parents=True, exist_ok=True)
        ai_flag.touch()
        with patch("hyperi_ci.upgrade._fetch_releases", return_value=SOAK_SAMPLE):
            with patch(
                "hyperi_ci.upgrade._run_upgrade_cmd", return_value=0
            ) as mock_run:
                with patch("hyperi_ci.upgrade._confirm_upgraded", return_value=True):
                    with patch("hyperi_ci.upgrade._re_exec"):
                        run_upgrade()
        mock_run.assert_called_once()


class TestRunUpgradeChannel:
    """The channel decides what an unpinned `hyperi-ci upgrade` installs."""

    def _upgrade_cmd(self) -> list[str]:
        with patch("hyperi_ci.upgrade._fetch_releases", return_value=SOAK_SAMPLE):
            with patch(
                "hyperi_ci.upgrade._run_upgrade_cmd", return_value=0
            ) as mock_run:
                with patch("hyperi_ci.upgrade._confirm_upgraded", return_value=True):
                    with patch("hyperi_ci.upgrade.shutil.which", return_value="/uv"):
                        run_upgrade()
        return list(mock_run.call_args[0][0])

    def test_does_not_re_exec(self) -> None:
        """Re-exec'ing `upgrade` runs it again, which loops on an older binary."""
        with patch("hyperi_ci.upgrade._fetch_releases", return_value=SOAK_SAMPLE):
            with patch("hyperi_ci.upgrade._run_upgrade_cmd", return_value=0):
                with patch("hyperi_ci.upgrade._confirm_upgraded", return_value=True):
                    with patch("hyperi_ci.upgrade._re_exec") as mock_exec:
                        assert run_upgrade() == 0
        mock_exec.assert_not_called()

    def test_live_installs_at_latest(self) -> None:
        channel.write_channel("live")
        assert "hyperi-ci@latest" in self._upgrade_cmd()

    def test_stable_installs_the_soaked_version_exactly(self) -> None:
        channel.write_channel("stable")
        assert "hyperi-ci==2.9.4" in self._upgrade_cmd()

    def test_does_not_downgrade_to_the_soaked_version(self) -> None:
        """Switching to stable while ahead holds the install, it does not roll back."""
        channel.write_channel("stable")
        with patch("hyperi_ci.upgrade._fetch_releases", return_value=SOAK_SAMPLE):
            with patch("hyperi_ci.upgrade.__version__", "2.9.6"):
                with patch("hyperi_ci.upgrade._run_upgrade_cmd") as mock_run:
                    assert run_upgrade() == 0
        mock_run.assert_not_called()

    def test_an_explicit_version_may_go_backwards(self) -> None:
        """A named version is a deliberate act, downgrade included."""
        with patch("hyperi_ci.upgrade.__version__", "2.9.6"):
            with patch(
                "hyperi_ci.upgrade._run_upgrade_cmd", return_value=0
            ) as mock_run:
                with patch("hyperi_ci.upgrade._confirm_upgraded", return_value=True):
                    with patch("hyperi_ci.upgrade._re_exec"):
                        with patch(
                            "hyperi_ci.upgrade.shutil.which", return_value="/uv"
                        ):
                            run_upgrade(version="2.9.4")
        assert "hyperi-ci==2.9.4" in list(mock_run.call_args[0][0])

    def test_fails_when_nothing_has_soaked(self) -> None:
        channel.write_channel("stable")
        fresh = _releases(**{"1.0.0": 1.0})
        with patch("hyperi_ci.upgrade._fetch_releases", return_value=fresh):
            with patch("hyperi_ci.upgrade._run_upgrade_cmd") as mock_run:
                assert run_upgrade() == 1
        mock_run.assert_not_called()

    def test_fails_when_pypi_is_unreadable(self) -> None:
        with patch("hyperi_ci.upgrade._fetch_releases", return_value={}):
            with patch("hyperi_ci.upgrade._run_upgrade_cmd") as mock_run:
                assert run_upgrade() == 1
        mock_run.assert_not_called()


class TestMaybeAutoUpdate:
    """The unattended path honours the same channel decision."""

    def _run(self, tmp_path: Path) -> list[str] | None:
        drop = (
            "CI",
            "GITHUB_ACTIONS",
            "GITLAB_CI",
            "JENKINS_URL",
            "BUILDKITE",
            "_HYPERCI_UPGRADING",
            "HYPERCI_AUTO_UPDATE",
        )
        env = {k: v for k, v in os.environ.items() if k not in drop}
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "ts"):
            with patch("hyperi_ci.upgrade.CACHE_DIR", tmp_path):
                with patch.dict(os.environ, env, clear=True):
                    with patch("sys.argv", ["hyperi-ci", "config"]):
                        with patch(
                            "hyperi_ci.upgrade._fetch_releases",
                            return_value=SOAK_SAMPLE,
                        ):
                            with patch("hyperi_ci.upgrade.__version__", "2.9.3"):
                                with patch(
                                    "hyperi_ci.upgrade._run_upgrade_cmd",
                                    return_value=0,
                                ) as mock_run:
                                    with patch(
                                        "hyperi_ci.upgrade._confirm_upgraded",
                                        return_value=True,
                                    ):
                                        with patch("hyperi_ci.upgrade._re_exec"):
                                            maybe_auto_update()
        if not mock_run.called:
            return None
        return list(mock_run.call_args[0][0])

    def test_live_auto_update_tracks_latest(self, tmp_path: Path) -> None:
        channel.write_channel("live")
        cmd = self._run(tmp_path)
        assert cmd is not None
        assert "hyperi-ci@latest" in cmd

    def test_stable_auto_update_installs_the_soaked_version(
        self, tmp_path: Path
    ) -> None:
        channel.write_channel("stable")
        cmd = self._run(tmp_path)
        assert cmd is not None
        assert "hyperi-ci==2.9.4" in cmd

    def test_frozen_does_nothing(self, tmp_path: Path) -> None:
        channel.freeze()
        assert self._run(tmp_path) is None

    def test_disabled_does_nothing(self, tmp_path: Path) -> None:
        channel.write_enabled(False)
        assert self._run(tmp_path) is None

    def test_stable_with_nothing_soaked_records_the_check(self, tmp_path: Path) -> None:
        """A check that ran and answered "wait" must not re-ask PyPI every call."""
        channel.write_channel("stable")
        drop = ("CI", "GITHUB_ACTIONS", "_HYPERCI_UPGRADING", "HYPERCI_AUTO_UPDATE")
        env = {k: v for k, v in os.environ.items() if k not in drop}
        fresh = _releases(**{"9.9.9": 1.0})
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "ts"):
            with patch("hyperi_ci.upgrade.CACHE_DIR", tmp_path):
                with patch.dict(os.environ, env, clear=True):
                    with patch("sys.argv", ["hyperi-ci", "config"]):
                        with patch(
                            "hyperi_ci.upgrade._fetch_releases", return_value=fresh
                        ):
                            with patch(
                                "hyperi_ci.upgrade._run_upgrade_cmd"
                            ) as mock_run:
                                maybe_auto_update()
        mock_run.assert_not_called()
        assert (tmp_path / "ts").is_file()

    def test_never_downgrades(self, tmp_path: Path) -> None:
        """Switching to stable while ahead of the soak window holds, not rolls back."""
        channel.write_channel("stable")
        drop = ("CI", "GITHUB_ACTIONS", "_HYPERCI_UPGRADING", "HYPERCI_AUTO_UPDATE")
        env = {k: v for k, v in os.environ.items() if k not in drop}
        with patch("hyperi_ci.upgrade.TIMESTAMP_FILE", tmp_path / "ts"):
            with patch("hyperi_ci.upgrade.CACHE_DIR", tmp_path):
                with patch.dict(os.environ, env, clear=True):
                    with patch("sys.argv", ["hyperi-ci", "config"]):
                        with patch(
                            "hyperi_ci.upgrade._fetch_releases",
                            return_value=SOAK_SAMPLE,
                        ):
                            with patch("hyperi_ci.upgrade.__version__", "2.9.6"):
                                with patch(
                                    "hyperi_ci.upgrade._run_upgrade_cmd"
                                ) as mock_run:
                                    maybe_auto_update()
        mock_run.assert_not_called()


class TestRunUpgradeCmd:
    """Run upgrade subprocess with permission error handling."""

    def test_success(self) -> None:
        with patch("hyperi_ci.upgrade.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            rc = _run_upgrade_cmd(["uv", "tool", "upgrade", "hyperi-ci"])
        assert rc == 0

    def test_nonzero_exit(self) -> None:
        with patch("hyperi_ci.upgrade.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            rc = _run_upgrade_cmd(["uv", "tool", "upgrade", "hyperi-ci"])
        assert rc == 1

    def test_permission_error_from_os(self) -> None:
        """OS raises PermissionError (e.g. no execute perms on uv)."""
        with patch(
            "hyperi_ci.upgrade.subprocess.run",
            side_effect=PermissionError("no perms"),
        ):
            rc = _run_upgrade_cmd(["uv", "tool", "upgrade", "hyperi-ci"])
        assert rc == 1

    def test_file_not_found(self) -> None:
        """Tool binary not found."""
        with patch(
            "hyperi_ci.upgrade.subprocess.run",
            side_effect=FileNotFoundError("uv"),
        ):
            rc = _run_upgrade_cmd(["uv", "tool", "upgrade", "hyperi-ci"])
        assert rc == 1
