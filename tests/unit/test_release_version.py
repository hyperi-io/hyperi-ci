# Project:   HyperI CI
# File:      tests/unit/test_release_version.py
# Purpose:   Tests for the shared release-version resolver (HYPERCI_VERSION-first)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""`resolve_release_version` — the single SSoT all stages use for the version
being released. HYPERCI_VERSION (Plan's next-version) wins over the committed
VERSION file, which is stale once stamping is central (#27 + zero-config)."""

from __future__ import annotations

from hyperi_ci.common import resolve_release_version


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
