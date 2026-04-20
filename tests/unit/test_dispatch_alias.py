# Project:   HyperI CI
# File:      tests/unit/test_dispatch_alias.py
# Purpose:   Unit tests for language aliasing in stage dispatch
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from hyperi_ci.dispatch import _find_handler_module


class TestJavascriptAliasesToTypescript:
    """JS projects with no tsconfig.json route through the TS handler package."""

    def test_quality_stage_maps_to_typescript_quality_module(self) -> None:
        js_mod = _find_handler_module("javascript", "quality")
        ts_mod = _find_handler_module("typescript", "quality")
        assert js_mod is not None
        assert js_mod is ts_mod

    def test_test_stage_maps_to_typescript_test_module(self) -> None:
        js_mod = _find_handler_module("javascript", "test")
        ts_mod = _find_handler_module("typescript", "test")
        assert js_mod is not None
        assert js_mod is ts_mod

    def test_build_stage_maps_to_typescript_build_module(self) -> None:
        js_mod = _find_handler_module("javascript", "build")
        ts_mod = _find_handler_module("typescript", "build")
        assert js_mod is not None
        assert js_mod is ts_mod

    def test_publish_stage_maps_to_typescript_publish_module(self) -> None:
        js_mod = _find_handler_module("javascript", "publish")
        ts_mod = _find_handler_module("typescript", "publish")
        assert js_mod is not None
        assert js_mod is ts_mod

    def test_typescript_still_resolves_to_itself(self) -> None:
        """Alias doesn't affect canonical names."""
        assert _find_handler_module("typescript", "quality") is not None

    def test_unknown_language_still_returns_none(self) -> None:
        """Only the javascript alias is wired — unknown names still fail."""
        assert _find_handler_module("bogus-lang", "quality") is None

    def test_unknown_stage_still_returns_none(self) -> None:
        """Non-existent stage on a real language still returns None."""
        assert _find_handler_module("typescript", "nonexistent-stage") is None
