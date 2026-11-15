# Project:   HyperI CI
# File:      tests/unit/test_dispatch_alias.py
# Purpose:   Unit tests for language aliasing in stage dispatch
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import pytest

from hyperi_ci.dispatch import _find_handler_module


class TestJavascriptAliasesToTypescript:
    """JS projects with no tsconfig.json route through the TS handler package."""

    @pytest.mark.parametrize("stage", ["quality", "test", "build", "release"])
    def test_a_stage_maps_to_the_typescript_module(self, stage: str) -> None:
        js_mod = _find_handler_module("javascript", stage)
        assert js_mod is not None
        assert js_mod is _find_handler_module("typescript", stage)

    def test_typescript_still_resolves_to_itself(self) -> None:
        """Alias doesn't affect canonical names."""
        assert _find_handler_module("typescript", "quality") is not None

    def test_unknown_language_still_returns_none(self) -> None:
        """Only the javascript alias is wired -- unknown names still fail."""
        assert _find_handler_module("bogus-lang", "quality") is None

    def test_unknown_stage_still_returns_none(self) -> None:
        """Non-existent stage on a real language still returns None."""
        assert _find_handler_module("typescript", "nonexistent-stage") is None
