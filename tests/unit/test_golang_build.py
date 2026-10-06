# Project:   HyperI CI
# File:      tests/unit/test_golang_build.py
# Purpose:   Unit tests for the Go build handler's target shortcuts
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from hyperi_ci.languages.golang.build import _expand_targets


class TestTargetShortcuts:
    """`all` covers Linux and macOS. Windows is built only when asked for."""

    def test_all_builds_no_windows_target(self) -> None:
        assert _expand_targets(["all"]) == [
            "linux/amd64",
            "linux/arm64",
            "darwin/amd64",
            "darwin/arm64",
        ]

    def test_windows_shortcut_still_expands(self) -> None:
        assert _expand_targets(["windows"]) == ["windows/amd64", "windows/arm64"]

    def test_an_explicit_target_passes_through(self) -> None:
        assert _expand_targets(["all", "windows/amd64"]) == [
            "linux/amd64",
            "linux/arm64",
            "darwin/amd64",
            "darwin/arm64",
            "windows/amd64",
        ]
