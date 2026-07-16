# Project:   HyperI CI
# File:      tests/unit/test_deprecated_files.py
# Purpose:   Tests for the config-driven deprecated-file hygiene nudge
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.quality import deprecated_files as dep


def test_table_loads_and_lists_releaserc_yaml() -> None:
    # The packaged table is the SSoT for "files hyperi-ci no longer wants".
    paths = {e["path"] for e in dep._load_table()}
    assert ".releaserc.yaml" in paths
    assert ".releaserc.yml" in paths


def test_scan_clean_repo_is_empty(tmp_path: Path) -> None:
    assert dep.scan(tmp_path) == []


def test_scan_flags_deprecated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".releaserc.yaml").write_text("branches: [main]\n", encoding="utf-8")
    monkeypatch.setattr(dep, "is_ci", lambda: False)
    assert dep.scan(tmp_path) == [".releaserc.yaml"]


def test_scan_reports_every_present_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".releaserc.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / ".releaserc.yml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.setattr(dep, "is_ci", lambda: False)
    assert set(dep.scan(tmp_path)) == {".releaserc.yaml", ".releaserc.yml"}


def test_scan_emits_github_annotation_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # In CI a warn also prints a ::warning:: so it escapes the folded log group.
    (tmp_path / ".releaserc.yaml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.setattr(dep, "is_ci", lambda: True)
    fired = dep.scan(tmp_path)
    out = capsys.readouterr().out
    assert fired == [".releaserc.yaml"]
    assert "::warning" in out
    assert ".releaserc.yaml" in out


def test_scan_no_annotation_when_not_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".releaserc.yaml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.setattr(dep, "is_ci", lambda: False)
    dep.scan(tmp_path)
    assert "::warning" not in capsys.readouterr().out


def test_missing_table_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A missing/unreadable table must never raise -- it is a nudge, not a gate.
    monkeypatch.setattr(dep, "_TABLE_PATH", tmp_path / "does-not-exist.yaml")
    assert dep.scan(tmp_path) == []
