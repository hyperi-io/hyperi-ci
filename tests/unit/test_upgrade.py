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

from hyperi_ci.upgrade import (
    CHECK_INTERVAL,
    _build_upgrade_cmd,
    _confirm_upgraded,
    _fetch_pypi_versions,
    _installed_version,
    _parse_installed_version,
    _parse_latest_version,
    _run_upgrade_cmd,
    _should_auto_update,
)


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
