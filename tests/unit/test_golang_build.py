# Project:   HyperI CI
# File:      tests/unit/test_golang_build.py
# Purpose:   Unit tests for the Go build handler's target shortcuts
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

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
