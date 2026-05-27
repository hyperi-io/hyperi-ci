# Project:   HyperI CI
# File:      tests/unit/test_quality_ignores.py
# Purpose:   Tests for generic quality ignore-list parser and per-language wiring
#
# License:   Proprietary -- HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for ``quality.ignore`` parsing and per-language translation."""

from __future__ import annotations

from datetime import date, timedelta

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


class TestBatchIds:
    """The ``ids`` list form expands one stanza into many entries."""

    def test_ids_list_expands_to_one_entry_per_id(self) -> None:
        raw = {
            "quality": {
                "ignore": [
                    {
                        "tool": "osv-scanner",
                        "ids": ["MAL-2026-4228", "MAL-2026-4359", "MAL-2026-4360"],
                        "reason": "ossf/malicious-packages#1276 FP wave",
                    }
                ]
            }
        }
        entries = load_ignores(raw)
        assert [e.id for e in entries] == [
            "MAL-2026-4228",
            "MAL-2026-4359",
            "MAL-2026-4360",
        ]
        assert all(e.tool == "osv-scanner" for e in entries)
        assert all("#1276" in e.reason for e in entries)

    def test_ids_entries_strip_whitespace(self) -> None:
        raw = {
            "quality": {
                "ignore": [
                    {
                        "tool": "osv-scanner",
                        "ids": ["  MAL-1  ", "MAL-2"],
                        "reason": "r",
                    }
                ]
            }
        }
        entries = load_ignores(raw)
        assert [e.id for e in entries] == ["MAL-1", "MAL-2"]

    def test_id_and_ids_both_accepted_and_merged(self) -> None:
        raw = {
            "quality": {
                "ignore": [
                    {
                        "tool": "osv-scanner",
                        "id": "MAL-0",
                        "ids": ["MAL-1", "MAL-2"],
                        "reason": "r",
                    }
                ]
            }
        }
        entries = load_ignores(raw)
        assert {e.id for e in entries} == {"MAL-0", "MAL-1", "MAL-2"}

    def test_ids_must_be_a_list(self) -> None:
        with pytest.raises(ValueError, match="ids"):
            load_ignores(
                {"quality": {"ignore": [{"tool": "x", "ids": "MAL-1", "reason": "r"}]}}
            )

    def test_neither_id_nor_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="id"):
            load_ignores({"quality": {"ignore": [{"tool": "x", "reason": "r"}]}})


class TestExpires:
    """Optional ``expires`` sunsets an ignore; framework-wide drop at load."""

    def test_future_expiry_is_kept(self) -> None:
        future = (date.today() + timedelta(days=30)).isoformat()
        raw = {
            "quality": {
                "ignore": [
                    {
                        "tool": "osv-scanner",
                        "id": "MAL-1",
                        "reason": "r",
                        "expires": future,
                    }
                ]
            }
        }
        entries = load_ignores(raw)
        assert len(entries) == 1
        assert entries[0].expires == date.today() + timedelta(days=30)

    def test_past_expiry_is_dropped(self) -> None:
        past = (date.today() - timedelta(days=1)).isoformat()
        raw = {
            "quality": {
                "ignore": [
                    {
                        "tool": "osv-scanner",
                        "id": "MAL-1",
                        "reason": "r",
                        "expires": past,
                    }
                ]
            }
        }
        assert load_ignores(raw) == []

    def test_expiry_today_is_kept(self) -> None:
        today = date.today().isoformat()
        raw = {
            "quality": {
                "ignore": [
                    {
                        "tool": "osv-scanner",
                        "id": "MAL-1",
                        "reason": "r",
                        "expires": today,
                    }
                ]
            }
        }
        assert len(load_ignores(raw)) == 1

    def test_no_expires_is_permanent(self) -> None:
        raw = {
            "quality": {
                "ignore": [{"tool": "pip-audit", "id": "PYSEC-1", "reason": "r"}]
            }
        }
        assert load_ignores(raw)[0].expires is None

    def test_invalid_expires_format_rejected(self) -> None:
        with pytest.raises(ValueError, match="expires"):
            load_ignores(
                {
                    "quality": {
                        "ignore": [
                            {
                                "tool": "x",
                                "id": "y",
                                "reason": "r",
                                "expires": "not-a-date",
                            }
                        ]
                    }
                }
            )

    def test_batch_with_expiry_drops_whole_stanza_when_past(self) -> None:
        past = (date.today() - timedelta(days=5)).isoformat()
        raw = {
            "quality": {
                "ignore": [
                    {
                        "tool": "osv-scanner",
                        "ids": ["MAL-1", "MAL-2"],
                        "reason": "FP wave",
                        "expires": past,
                    }
                ]
            }
        }
        assert load_ignores(raw) == []

    def test_lapsed_entry_is_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr("hyperi_ci.quality.ignores.warn", lambda m: calls.append(m))
        past = (date.today() - timedelta(days=1)).isoformat()
        load_ignores(
            {
                "quality": {
                    "ignore": [
                        {
                            "tool": "osv-scanner",
                            "id": "MAL-9",
                            "reason": "r",
                            "expires": past,
                        }
                    ]
                }
            }
        )
        assert any("MAL-9" in c for c in calls)


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
