# Project:   HyperI CI
# File:      tests/unit/test_deprecated_files.py
# Purpose:   Tests for the config-driven deprecated-file hygiene nudge
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
from pathlib import Path

import pytest

from hyperi_ci import common
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
    monkeypatch.setattr(common, "is_github_actions", lambda: False)
    assert dep.scan(tmp_path) == [".releaserc.yaml"]


def test_scan_reports_every_present_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".releaserc.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / ".releaserc.yml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.setattr(common, "is_github_actions", lambda: False)
    assert set(dep.scan(tmp_path)) == {".releaserc.yaml", ".releaserc.yml"}


def test_scan_emits_one_github_annotation_under_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The annotation reaches the run summary; a logger warning there would be
    # a second annotation for the same file.
    said: list[str] = []
    (tmp_path / ".releaserc.yaml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.setattr(common, "is_github_actions", lambda: True)
    monkeypatch.setattr(common, "warn", said.append)
    fired = dep.scan(tmp_path)
    out = capsys.readouterr().out
    assert fired == [".releaserc.yaml"]
    warnings = [line for line in out.splitlines() if line.startswith("::warning")]
    assert len(warnings) == 1, out
    assert warnings[0].startswith("::warning title=hyperi-ci deprecated file::")
    assert ".releaserc.yaml" in warnings[0]
    assert said == []


def test_scan_is_a_log_line_off_github_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    said: list[str] = []
    (tmp_path / ".releaserc.yaml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.setattr(common, "is_github_actions", lambda: False)
    monkeypatch.setattr(common, "warn", said.append)
    dep.scan(tmp_path)
    assert "::warning" not in capsys.readouterr().out
    assert len(said) == 1, said
    assert ".releaserc.yaml" in said[0]


def test_missing_table_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A missing/unreadable table must never raise - it is a nudge, not a gate.
    monkeypatch.setattr(dep, "_TABLE_PATH", tmp_path / "does-not-exist.yaml")
    assert dep.scan(tmp_path) == []


def test_malformed_table_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unparseable YAML (yaml.YAMLError, NOT a ValueError) must not raise.
    bad = tmp_path / "bad.yaml"
    bad.write_text("files: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(dep, "_TABLE_PATH", bad)
    assert dep.scan(tmp_path) == []


def test_non_mapping_table_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Valid YAML but a top-level list (not a mapping) must not crash on .get().
    listy = tmp_path / "list.yaml"
    listy.write_text("- a\n- b\n", encoding="utf-8")
    monkeypatch.setattr(dep, "_TABLE_PATH", listy)
    assert dep.scan(tmp_path) == []
