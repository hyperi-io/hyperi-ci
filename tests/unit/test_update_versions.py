# Project:   HyperI CI
# File:      tests/unit/test_update_versions.py
# Purpose:   Tests for the action-version SSOT sync regexes
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/update-versions.py version-pin regexes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "update_versions",
    Path(__file__).resolve().parents[2] / "scripts" / "update-versions.py",
)
update_versions = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(update_versions)


def _apply(text: str, versions: dict) -> str:
    for pattern, replacement, _desc in update_versions._build_replacements(versions):
        text = pattern.sub(replacement, text)
    return text


class TestSemanticReleasePin:
    def test_pins_bare_npm_package(self) -> None:
        out = _apply(
            "npm i -g semantic-release@20", {"semantic_release": {"core": "25"}}
        )
        assert "semantic-release@25" in out

    def test_does_not_touch_setup_semantic_release_action_ref(self) -> None:
        # Regression: the action name ends in "semantic-release"; the npm pin
        # regex must not rewrite the action ref's @main to @25.
        ref = "uses: hyperi-io/hyperi-ci/.github/actions/setup-semantic-release@main"
        out = _apply(ref, {"semantic_release": {"core": "25"}})
        assert out == ref
