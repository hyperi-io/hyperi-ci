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
skip short-circuits in ``run`` (which return before any scan); and the
automatic exclude of the ``python.lang.compatibility.*`` rules a
project's own ``requires-python`` floor has outgrown.
"""

import subprocess
from pathlib import Path

import pytest

from hyperi_ci import common
from hyperi_ci.config import CIConfig
from hyperi_ci.languages import quality_common
from hyperi_ci.languages.quality_common import GateReasonRequiredError
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
    """Start each test with strict + skip unset, as on a workstation.

    Under GitHub Actions a relaxed gate is an annotation instead of a log line,
    so a test reading the log line must not depend on the runner it runs on.
    """
    monkeypatch.delenv(_STRICT, raising=False)
    monkeypatch.delenv(_SKIP, raising=False)
    monkeypatch.setattr(common, "is_github_actions", lambda: False)


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

    def test_legacy_key_is_measured_against_the_shipped_default(self) -> None:
        # quality.python.semgrep carries no default of its own, so without the
        # fallback a repo could disable SAST through it unremarked.
        cfg = _cfg({"quality": {"python": {"semgrep": "disabled"}}})
        with pytest.raises(GateReasonRequiredError, match="quality.python.semgrep"):
            semgrep._resolve_mode(cfg, "python")

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
        monkeypatch.setattr(common, "warn", said.append)
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

    def test_disabled_without_a_reason_raises(self) -> None:
        cfg = _cfg({"quality": {"semgrep": "disabled"}})
        with pytest.raises(GateReasonRequiredError, match="security gate"):
            semgrep.run(cfg)


class TestStaleCompatRules:
    """20 ids from `r/python.lang.compatibility` (semgrep 1.178.0), each
    targeting python36 or python37 - verified by `semgrep scan --config
    r/python.lang.compatibility --verbose` against an empty probe file.
    """

    def test_no_floor_excludes_nothing(self) -> None:
        assert semgrep._stale_compat_rules(None) == []

    def test_a_floor_below_the_family_excludes_nothing(self) -> None:
        assert semgrep._stale_compat_rules("3.2") == []

    def test_a_floor_at_python36_excludes_only_that_family(self) -> None:
        excluded = semgrep._stale_compat_rules("3.6")
        expected = {
            rid
            for rid, target in semgrep._PYTHON_COMPAT_RULES.items()
            if target == "3.6"
        }
        assert set(excluded) == expected
        assert len(excluded) == 3

    def test_a_floor_above_both_families_excludes_all_20(self) -> None:
        excluded = semgrep._stale_compat_rules("3.14")
        assert set(excluded) == set(semgrep._PYTHON_COMPAT_RULES)
        assert len(excluded) == 20


def _exclude_rule_ids(cmd: list[str]) -> set[str]:
    return {cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--exclude-rule"}


class TestCompatExcludesInArgv:
    """argv-level proof: the exclude only shows up once a floor earns it."""

    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(
            cmd: list[str], *_a: object, **_k: object
        ) -> subprocess.CompletedProcess:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(semgrep.subprocess, "run", fake_run)
        return calls

    def test_a_314_floor_gets_every_compat_rule_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nrequires-python = ">=3.14"\n', encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        calls = self._capture(monkeypatch)

        assert semgrep.run(_cfg()) == 0
        [cmd] = calls
        assert set(semgrep._PYTHON_COMPAT_RULES).issubset(_exclude_rule_ids(cmd))

    def test_no_declared_floor_keeps_every_compat_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(
            tmp_path
        )  # no pyproject.toml -> requires_python_floor() is None
        calls = self._capture(monkeypatch)

        assert semgrep.run(_cfg()) == 0
        [cmd] = calls
        assert _exclude_rule_ids(cmd).isdisjoint(semgrep._PYTHON_COMPAT_RULES)

    def test_a_floor_below_the_family_keeps_every_compat_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nrequires-python = ">=3.2"\n', encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        calls = self._capture(monkeypatch)

        assert semgrep.run(_cfg()) == 0
        [cmd] = calls
        assert _exclude_rule_ids(cmd).isdisjoint(semgrep._PYTHON_COMPAT_RULES)
