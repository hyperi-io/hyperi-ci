# Project:   HyperI CI
# File:      tests/unit/test_upgrade.py
# Purpose:   Tests for self-upgrade functionality
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

from hyperi_ci.upgrade import (
    CHECK_INTERVAL,
    _build_upgrade_cmd,
    _parse_latest_version,
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

    def test_uv_latest(self) -> None:
        cmd = _build_upgrade_cmd(uv_path="/usr/bin/uv", version=None, pre=False)
        assert cmd == ["/usr/bin/uv", "tool", "upgrade", "hyperi-ci"]

    def test_uv_pinned(self) -> None:
        cmd = _build_upgrade_cmd(uv_path="/usr/bin/uv", version="1.2.0", pre=False)
        assert cmd == [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            "hyperi-ci==1.2.0",
        ]

    def test_uv_pre(self) -> None:
        cmd = _build_upgrade_cmd(uv_path="/usr/bin/uv", version=None, pre=True)
        assert cmd == [
            "/usr/bin/uv",
            "tool",
            "upgrade",
            "--prerelease=allow",
            "hyperi-ci",
        ]

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
