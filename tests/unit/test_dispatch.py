# Project:   HyperI CI
# File:      tests/unit/test_dispatch.py
# Purpose:   Unit tests for the stage dispatcher
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci import dispatch


class TestProjectDirReachesTheHandlers:
    """`-C` has to move the process, because handlers run tools in cwd.

    `hyperi-ci run quality -C <other-repo>` resolved the root for language
    detection and config, then ran cargo/uv/npm wherever the shell happened
    to be (issue #109).
    """

    @staticmethod
    def _record_chdir(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        seen: list[Path] = []
        monkeypatch.setattr("os.chdir", lambda path: seen.append(Path(path)))
        monkeypatch.setattr(dispatch, "detect_language", lambda _dir: None)
        return seen

    def test_chdir_to_project_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._record_chdir(monkeypatch)
        assert dispatch.run_stage("quality", project_dir=tmp_path) == 1
        assert seen == [tmp_path.resolve()]

    def test_chdir_to_cwd_when_no_project_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._record_chdir(monkeypatch)
        assert dispatch.run_stage("quality") == 1
        assert seen == [Path.cwd().resolve()]

    def test_unknown_stage_does_not_move_the_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._record_chdir(monkeypatch)
        assert dispatch.run_stage("nonsense", project_dir=tmp_path) == 1
        assert seen == []
