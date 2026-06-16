# Project:   HyperI CI
# File:      tests/unit/test_release_version.py
# Purpose:   Tests for the shared release-version resolver (HYPERCI_VERSION-first)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""`resolve_release_version` — the single SSoT all stages use for the version
being released. HYPERCI_VERSION (Plan's next-version) wins over the committed
VERSION file, which is stale once stamping is central (#27 + zero-config)."""

from __future__ import annotations

import pytest

from hyperi_ci.common import explicit_version, resolve_release_version


def test_hyperci_version_wins_and_strips_v(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HYPERCI_VERSION", "v1.2.3")
    (tmp_path / "VERSION").write_text("9.9.9\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_release_version() == "1.2.3"


def test_version_file_fallback_strips_v(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HYPERCI_VERSION", raising=False)
    (tmp_path / "VERSION").write_text("v4.5.6\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_release_version() == "4.5.6"


def test_none_when_neither_present(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HYPERCI_VERSION", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_release_version() is None


def test_empty_hyperci_version_ignored(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HYPERCI_VERSION", "   ")
    (tmp_path / "VERSION").write_text("7.0.0\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_release_version() == "7.0.0"


class TestExplicitVersion:
    """`explicit_version` distinguishes a `--version X.Y.Z` override from a
    bump level travelling in the same `bump` channel (issue #37)."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1.18.4", "1.18.4"),
            ("v1.18.4", "1.18.4"),  # leading v tolerated + stripped
            ("  1.18.4  ", "1.18.4"),  # whitespace trimmed
            ("0.0.0", "0.0.0"),
            ("12.345.6789", "12.345.6789"),
        ],
    )
    def test_accepts_plain_semver(self, value: str, expected: str) -> None:
        assert explicit_version(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "auto",
            "patch",
            "minor",
            "1.2",  # too few components
            "1.2.3.4",  # too many
            "1.2.x",
            "1.2.3-rc1",  # no pre-release metadata
            "v",
            "latest",
        ],
    )
    def test_rejects_non_semver(self, value: str | None) -> None:
        assert explicit_version(value) is None
