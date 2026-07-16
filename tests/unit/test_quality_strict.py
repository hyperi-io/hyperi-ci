# Project:   HyperI CI
# File:      tests/unit/test_quality_strict.py
# Purpose:   Tests for strict quality mode (warn-tier findings -> blocking)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for strict quality mode.

Covers `hyperi_ci.languages.quality_common.strict_quality` and
`resolve_tool_mode` - the shared machinery behind `hyperi-ci check
--strict`, which upgrades warn-tier findings (ty, semgrep, docstrings)
to blocking so they surface before a push instead of after.
"""

from __future__ import annotations

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import (
    is_skipped,
    quality_skip,
    resolve_tool_mode,
    strict_quality,
)

_ENV = "HYPERCI_QUALITY_STRICT"
_SKIP = "HYPERCI_QUALITY_SKIP"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from ambient strict/skip env vars."""
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.delenv(_SKIP, raising=False)


def _config(tool: str, mode: str, language: str = "python") -> CIConfig:
    return CIConfig(_raw={"quality": {language: {tool: mode}}})


class TestStrictQuality:
    """The env-driven strict switch."""

    def test_unset_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        assert strict_quality() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "on", " on "])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(_ENV, val)
        assert strict_quality() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "", "off", "maybe"])
    def test_falsey_values(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(_ENV, val)
        assert strict_quality() is False


class TestResolveToolMode:
    """Mode resolution, with and without strict."""

    def test_default_is_blocking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        assert resolve_tool_mode("ty", CIConfig(_raw={}), "python") == "blocking"

    def test_configured_mode_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        warn_cfg = _config("ty", "warn")
        off_cfg = _config("bandit", "disabled")
        assert resolve_tool_mode("ty", warn_cfg, "python") == "warn"
        assert resolve_tool_mode("bandit", off_cfg, "python") == "disabled"

    def test_strict_upgrades_warn_to_blocking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_ENV, "1")
        assert resolve_tool_mode("ty", _config("ty", "warn"), "python") == "blocking"

    def test_strict_leaves_disabled_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Strict enforces warnings; it must NOT resurrect a disabled tool.
        monkeypatch.setenv(_ENV, "1")
        off_cfg = _config("bandit", "disabled")
        assert resolve_tool_mode("bandit", off_cfg, "python") == "disabled"

    def test_strict_leaves_blocking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, "1")
        block_cfg = _config("ruff", "blocking")
        assert resolve_tool_mode("ruff", block_cfg, "python") == "blocking"

    @pytest.mark.parametrize("language", ["python", "rust", "golang", "typescript"])
    def test_strict_applies_across_languages(
        self, monkeypatch: pytest.MonkeyPatch, language: str
    ) -> None:
        monkeypatch.setenv(_ENV, "1")
        cfg = _config("semgrep", "warn", language)
        assert resolve_tool_mode("semgrep", cfg, language) == "blocking"


class TestQualitySkip:
    """HYPERCI_QUALITY_SKIP: the rare force-skip escape hatch."""

    def test_unset_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_SKIP, raising=False)
        assert quality_skip() == frozenset()

    def test_parses_comma_separated_lowercased(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_SKIP, "Semgrep, Bandit ,")
        assert quality_skip() == frozenset({"semgrep", "bandit"})

    def test_is_skipped_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_SKIP, "semgrep")
        assert is_skipped("semgrep") is True
        assert is_skipped("SEMGREP") is True
        assert is_skipped("ruff") is False

    def test_skip_disables_even_a_blocking_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        monkeypatch.setenv(_SKIP, "ruff")
        assert resolve_tool_mode("ruff", _config("ruff", "blocking"), "python") == (
            "disabled"
        )

    def test_skip_wins_over_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both set: skip wins - the tool is disabled, not upgraded to blocking.
        monkeypatch.setenv(_ENV, "1")
        monkeypatch.setenv(_SKIP, "ty")
        assert resolve_tool_mode("ty", _config("ty", "warn"), "python") == "disabled"
