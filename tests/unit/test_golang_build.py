# Project:   HyperI CI
# File:      tests/unit/test_golang_build.py
# Purpose:   Unit tests for the Go build handler's target shortcuts
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import subprocess

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.golang import build
from hyperi_ci.languages.golang.build import _expand_targets, windows_targets


class TestWindowsIsRefused:
    """A Windows target fails at target resolution, before anything builds."""

    def test_windows_targets_are_found(self) -> None:
        assert windows_targets(["linux/amd64", "windows/amd64"]) == ["windows/amd64"]

    def test_build_fails_early_on_a_windows_target(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(build.shutil, "which", lambda _name: "/usr/bin/go")
        errors: list[str] = []
        monkeypatch.setattr(build, "error", errors.append)
        config = CIConfig(_raw={"build": {"golang": {"targets": ["windows/amd64"]}}})

        assert build.run(config) == 1
        assert "Windows targets are not supported" in errors[0]
        assert not (tmp_path / "dist").exists()


class TestEnvOverridesReachTheBuild:
    """GO_VERSION_PKG, GO_BINARY_NAME and GO_BUILD_TAGS come from the process env."""

    def test_documented_env_vars_reach_go_build(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(build.shutil, "which", lambda _name: "/usr/bin/go")
        monkeypatch.setattr(build, "_detect_version", lambda: "1.2.3")
        monkeypatch.setenv("GO_VERSION_PKG", "example.com/app/version")
        monkeypatch.setenv("GO_BINARY_NAME", "app")
        monkeypatch.setenv("GO_BUILD_TAGS", "netgo")
        monkeypatch.setenv("GO_MAIN_PKG", "./cmd/app")
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "abc1234\n", "")

        monkeypatch.setattr(build.subprocess, "run", fake_run)
        config = CIConfig(_raw={"build": {"golang": {"targets": ["linux/amd64"]}}})

        assert build.run(config) == 0
        (cmd,) = [c for c in calls if c[:2] == ["go", "build"]]
        ldflags = cmd[cmd.index("-ldflags") + 1]
        assert "-X 'example.com/app/version.Version=1.2.3'" in ldflags
        assert cmd[cmd.index("-tags") + 1] == "netgo"
        assert cmd[cmd.index("-o") + 1] == "dist/app-linux-amd64"


class TestTargetShortcuts:
    """`all` covers Linux and macOS, the only platforms built."""

    def test_all_builds_linux_and_darwin(self) -> None:
        assert _expand_targets(["all"]) == [
            "linux/amd64",
            "linux/arm64",
            "darwin/amd64",
            "darwin/arm64",
        ]

    def test_there_is_no_windows_shortcut(self) -> None:
        assert _expand_targets(["windows"]) == ["windows"]

    def test_an_explicit_target_passes_through(self) -> None:
        assert _expand_targets(["linux", "darwin/arm64"]) == [
            "linux/amd64",
            "linux/arm64",
            "darwin/arm64",
        ]
