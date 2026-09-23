# Project:   HyperI CI
# File:      tests/unit/test_semgrep.py
# Purpose:   Tests for the dispatch-level semgrep quality module
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the centralised (dispatch-level) semgrep module.

Semgrep moved out of the per-language handlers to run once, cross-
language, like gitleaks. These cover mode resolution - the new
``quality.semgrep`` key, the legacy per-language back-compat override,
strict upgrade, and the force-skip escape hatch - plus the disabled/
skip short-circuits in ``run`` (which return before any scan).
"""

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages import quality_common
from hyperi_ci.quality import semgrep

_STRICT = "HYPERCI_QUALITY_STRICT"
_SKIP = "HYPERCI_QUALITY_SKIP"

# The legacy per-language key, set to the mode semgrep already ships.
_WARN = {"semgrep": "warn"}


def _cfg(raw: dict | None = None) -> CIConfig:
    return CIConfig(_raw=raw or {})


def _off(reason: str) -> dict[str, str]:
    """A semgrep gate turned below the shipped `warn`, with its stated reason."""
    return {"mode": "disabled", "reason": reason}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with strict + skip unset."""
    monkeypatch.delenv(_STRICT, raising=False)
    monkeypatch.delenv(_SKIP, raising=False)


class TestResolveMode:
    def test_default_is_warn(self) -> None:
        assert semgrep._resolve_mode(_cfg(), None) == "warn"

    def test_top_level_key(self) -> None:
        cfg = _cfg({"quality": {"semgrep": "blocking"}})
        assert semgrep._resolve_mode(cfg, None) == "blocking"

    def test_writing_the_shipped_warn_needs_no_reason(self) -> None:
        # semgrep ships `warn`, so writing it is not a downgrade (issue #250).
        cfg = _cfg({"quality": {"semgrep": "warn"}})
        assert semgrep._resolve_mode(cfg, None) == "warn"
        assert semgrep._resolve_mode(
            _cfg({"quality": {"python": _WARN}}), "python"
        ) == ("warn")

    def test_legacy_per_language_override_wins(self) -> None:
        # Back-compat: a consumer's old quality.<lang>.semgrep still applies.
        cfg = _cfg({"quality": {"python": {"semgrep": _off("no SAST rules for this")}}})
        assert semgrep._resolve_mode(cfg, "python") == "disabled"

    def test_legacy_key_is_measured_against_the_shipped_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # quality.python.semgrep carries no default of its own, so without the
        # fallback a repo could disable SAST through it unremarked.
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        cfg = _cfg({"quality": {"python": {"semgrep": "disabled"}}})
        semgrep._resolve_mode(cfg, "python")
        assert any("quality.python.semgrep" in w for w in said), said

    @pytest.mark.parametrize(
        "raw",
        ["block", "enabled", {"mode": "off", "reason": "typo"}],
    )
    def test_an_unknown_mode_is_named_and_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: object
    ) -> None:
        # The shared resolvers reject a typo, so semgrep must not be the one
        # gate that carries it through as a mode nothing else recognises.
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        cfg = _cfg({"quality": {"semgrep": raw}})
        assert semgrep._resolve_mode(cfg, None) == "warn"
        assert any("unknown mode" in w for w in said), said

    def test_strict_upgrades_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_STRICT, "1")
        assert semgrep._resolve_mode(_cfg(), None) == "blocking"

    def test_skip_wins_over_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_STRICT, "1")
        monkeypatch.setenv(_SKIP, "semgrep")
        assert semgrep._resolve_mode(_cfg(), None) == "disabled"


class TestRun:
    def test_force_skip_short_circuits_to_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force-skipped -> disabled -> returns 0 before any scan runs.
        monkeypatch.setenv(_SKIP, "semgrep")
        assert semgrep.run(_cfg()) == 0

    def test_disabled_config_returns_zero(self) -> None:
        cfg = _cfg({"quality": {"semgrep": _off("no SAST on this repo")}})
        assert semgrep.run(cfg) == 0

    def test_disabled_without_a_reason_is_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        cfg = _cfg({"quality": {"semgrep": "disabled"}})
        assert semgrep.run(cfg) == 0
        assert any("security gate" in w for w in said), said
