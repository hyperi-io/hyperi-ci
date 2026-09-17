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
from hyperi_ci.dispatch import _find_handler_module


class TestPublishIsAnAliasForRelease:
    """hyperi-io/vector-vrl calls `hyperi-ci run publish` from its own
    workflow, so the old stage name resolves to the release handler."""

    def test_the_alias_reaches_the_same_handler_module(self) -> None:
        assert _find_handler_module("python", "publish") is _find_handler_module(
            "python", "release"
        )

    def test_both_names_are_accepted_stages(self) -> None:
        assert {"release", "publish"} <= set(dispatch.VALID_STAGES)

    def test_the_alias_is_not_rejected_as_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unknown stage returns before the chdir, so reaching it proves
        # `publish` resolved rather than being rejected.
        seen: list[Path] = []
        monkeypatch.setattr("os.chdir", lambda path: seen.append(Path(path)))
        monkeypatch.setattr(dispatch, "detect_language", lambda _dir: None)
        assert dispatch.run_stage("publish", project_dir=tmp_path) == 1
        assert seen == [tmp_path.resolve()]


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
