# Project:   HyperI CI
# File:      tests/unit/test_quality_ignores.py
# Purpose:   Tests for generic quality ignore-list parser and per-language wiring
#
# License:   Proprietary -- HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for ``quality.ignore`` parsing and per-language translation."""

from __future__ import annotations

import pytest

from hyperi_ci.quality.ignores import IgnoreEntry, for_tool, load_ignores


class TestLoadIgnores:
    """Parse quality.ignore from a raw .hyperi-ci.yaml dict."""

    def test_empty_config_returns_empty_list(self) -> None:
        assert load_ignores({}) == []

    def test_missing_quality_section_returns_empty(self) -> None:
        assert load_ignores({"build": {"type": "package"}}) == []

    def test_missing_ignore_returns_empty(self) -> None:
        assert load_ignores({"quality": {"python": {"ruff": "blocking"}}}) == []

    def test_single_entry_parses(self) -> None:
        raw = {
            "quality": {
                "ignore": [
                    {
                        "tool": "pip-audit",
                        "id": "PYSEC-2025-183",
                        "reason": "Disputed CVE",
                    }
                ]
            }
        }
        entries = load_ignores(raw)
        assert len(entries) == 1
        assert entries[0] == IgnoreEntry(
            tool="pip-audit", id="PYSEC-2025-183", reason="Disputed CVE"
        )

    def test_multiple_entries_for_different_tools(self) -> None:
        raw = {
            "quality": {
                "ignore": [
                    {"tool": "pip-audit", "id": "PYSEC-1", "reason": "r1"},
                    {"tool": "cargo-audit", "id": "RUSTSEC-1", "reason": "r2"},
                    {"tool": "semgrep", "id": "rule-x", "reason": "r3"},
                ]
            }
        }
        entries = load_ignores(raw)
        assert {e.tool for e in entries} == {"pip-audit", "cargo-audit", "semgrep"}

    def test_strips_whitespace(self) -> None:
        raw = {
            "quality": {
                "ignore": [
                    {
                        "tool": "  pip-audit  ",
                        "id": "  PYSEC-1  ",
                        "reason": "  spaced  ",
                    }
                ]
            }
        }
        entries = load_ignores(raw)
        assert entries[0] == IgnoreEntry(
            tool="pip-audit", id="PYSEC-1", reason="spaced"
        )

    def test_ignore_must_be_a_list(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            load_ignores({"quality": {"ignore": "not-a-list"}})

    def test_entry_must_be_a_mapping(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            load_ignores({"quality": {"ignore": ["bare-string"]}})

    def test_missing_tool_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="tool"):
            load_ignores({"quality": {"ignore": [{"id": "X", "reason": "y"}]}})

    def test_missing_id_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="id"):
            load_ignores(
                {"quality": {"ignore": [{"tool": "pip-audit", "reason": "y"}]}}
            )

    def test_missing_reason_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            load_ignores({"quality": {"ignore": [{"tool": "pip-audit", "id": "X"}]}})

    def test_blank_reason_rejected(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            load_ignores(
                {
                    "quality": {
                        "ignore": [{"tool": "pip-audit", "id": "X", "reason": ""}]
                    }
                }
            )


class TestForTool:
    """Filter ignore entries by tool slug."""

    def test_returns_only_matching(self) -> None:
        entries = [
            IgnoreEntry("pip-audit", "PYSEC-1", "r1"),
            IgnoreEntry("cargo-audit", "RUSTSEC-1", "r2"),
            IgnoreEntry("pip-audit", "PYSEC-2", "r3"),
        ]
        result = for_tool(entries, "pip-audit")
        assert [e.id for e in result] == ["PYSEC-1", "PYSEC-2"]

    def test_returns_empty_for_unknown_tool(self) -> None:
        entries = [IgnoreEntry("pip-audit", "PYSEC-1", "r1")]
        assert for_tool(entries, "cargo-audit") == []

    def test_returns_empty_from_empty_input(self) -> None:
        assert for_tool([], "pip-audit") == []
