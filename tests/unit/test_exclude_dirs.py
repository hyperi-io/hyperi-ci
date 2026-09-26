# Project:   HyperI CI
# File:      tests/unit/test_exclude_dirs.py
# Purpose:   Tests for the shared quality exclude list
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""`get_exclude_dirs` and what it hands the target discovery."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from hyperi_ci import common
from hyperi_ci.common import get_exclude_dirs
from hyperi_ci.quality.targets import discover_markdown_files


def _config(*entries: str) -> dict:
    return {"quality": {"exclude_paths": list(entries)}}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fresh working tree as cwd, with the once-per-process warning reset."""
    monkeypatch.chdir(tmp_path)
    common._warn_unmatched_excludes.cache_clear()
    yield tmp_path
    common._warn_unmatched_excludes.cache_clear()


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect what get_exclude_dirs warns. Loguru bypasses capsys/caplog."""
    lines: list[str] = []
    monkeypatch.setattr(common, "warn", lines.append)
    return lines


class TestConfiguredExcludes:
    """issue #338: a bare name was dropped unless the repo root had it too."""

    def test_bare_name_kept_when_only_nested(
        self, repo: Path, warnings: list[str]
    ) -> None:
        (repo / "src" / "pkg" / "data").mkdir(parents=True)
        assert "data" in get_exclude_dirs(_config("data"))
        assert warnings == []

    def test_bare_name_prunes_the_nested_dir_in_discovery(self, repo: Path) -> None:
        nested = repo / "src" / "pkg" / "data"
        nested.mkdir(parents=True)
        (nested / "fixture.md").write_text("# data\n", encoding="utf-8")
        (repo / "README.md").write_text("# repo\n", encoding="utf-8")
        excludes = get_exclude_dirs(_config("data"))
        found = discover_markdown_files(repo, exclude_dirs=excludes)
        assert found == [repo / "README.md"]

    def test_path_entry_dropped_when_not_a_dir(
        self, repo: Path, warnings: list[str]
    ) -> None:
        (repo / "docs").mkdir()
        assert "docs/missing" not in get_exclude_dirs(_config("docs/missing"))
        assert len(warnings) == 1
        assert "docs/missing" in warnings[0]

    def test_unmatched_bare_name_kept_and_warned_once(
        self, repo: Path, warnings: list[str]
    ) -> None:
        (repo / "src").mkdir()
        first = get_exclude_dirs(_config("dtaa"))
        second = get_exclude_dirs(_config("dtaa"))
        assert "dtaa" in first
        assert "dtaa" in second
        assert len(warnings) == 1
        assert "dtaa" in warnings[0]

    def test_bare_glob_matching_a_nested_dir_is_not_warned(
        self, repo: Path, warnings: list[str]
    ) -> None:
        (repo / "src" / "pkg.egg-info").mkdir(parents=True)
        assert "*.egg-info" in get_exclude_dirs(_config("*.egg-info"))
        assert warnings == []

    def test_name_inside_a_skipped_dir_counts_as_unmatched(
        self, repo: Path, warnings: list[str]
    ) -> None:
        (repo / "node_modules" / "pkg" / "fixtures").mkdir(parents=True)
        get_exclude_dirs(_config("fixtures"))
        assert len(warnings) == 1


class TestRootEntriesUnchanged:
    """Entries that exist from the root behave as they did before #338."""

    def test_root_dir_and_nested_path_kept_without_warning(
        self, repo: Path, warnings: list[str]
    ) -> None:
        (repo / "data").mkdir()
        (repo / "docs" / "api").mkdir(parents=True)
        excludes = get_exclude_dirs(_config("data", "docs/api"))
        assert "data" in excludes
        assert "docs/api" in excludes
        assert warnings == []

    def test_builtin_excludes_still_need_to_exist(self, repo: Path) -> None:
        (repo / "node_modules").mkdir()
        excludes = get_exclude_dirs()
        assert "node_modules" in excludes
        assert "target" not in excludes
        assert "ci" not in excludes

    def test_entry_listed_once(self, repo: Path) -> None:
        (repo / "node_modules").mkdir()
        excludes = get_exclude_dirs(_config("node_modules", "node_modules"))
        assert excludes.count("node_modules") == 1
