# Project:   HyperI CI
# File:      tests/unit/test_dispatch.py
# Purpose:   Unit tests for the stage dispatcher
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hyperi_ci import dispatch
from hyperi_ci.config import CIConfig
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


class TestLocalGatesCloseTheCiOnlyHole:
    """A gate that runs only as a CI job makes a local green a lie. A repo
    declares those commands and gets them back in `hyperi-ci check`."""

    @staticmethod
    def _config(gates: object) -> CIConfig:
        return CIConfig(_raw={"quality": {"local_gates": gates}})

    def test_no_declaration_is_a_clean_no_op(self) -> None:
        assert dispatch._run_local_gates(CIConfig(_raw={})) == 0

    def test_a_passing_gate_returns_zero(self) -> None:
        assert (
            dispatch._run_local_gates(
                self._config([{"name": "ok", "command": ["true"]}])
            )
            == 0
        )

    def test_a_failing_gate_fails_the_check(self) -> None:
        """The whole point: CI would have failed, so the local check must."""
        assert (
            dispatch._run_local_gates(
                self._config([{"name": "boom", "command": ["false"]}])
            )
            != 0
        )

    def test_a_malformed_entry_fails_loudly_rather_than_skipping(self) -> None:
        """A silently skipped gate is the defect this exists to close."""
        assert dispatch._run_local_gates(self._config([{"name": "no command"}])) == 1
        assert dispatch._run_local_gates(self._config([{"command": ["true"]}])) == 1
        assert dispatch._run_local_gates(self._config(["not-a-mapping"])) == 1

    def test_the_first_failure_stops_the_rest(self) -> None:
        gates = [
            {"name": "first", "command": ["false"]},
            {"name": "second", "command": ["true"]},
        ]
        assert dispatch._run_local_gates(self._config(gates)) != 0

    def test_this_repo_declares_its_two_ci_only_gates(self) -> None:
        """versions SSOT and workflow interfaces are blocking jobs in ci.yml
        with no other local path. Read by PATH, not `load_config()`, which
        resolves against the working directory and made this pass alone and
        fail in the suite."""
        root = Path(__file__).resolve().parents[2]
        declared = yaml.safe_load(
            (root / ".hyperi-ci.yaml").read_text(encoding="utf-8")
        )["quality"]["local_gates"]
        assert {g["name"] for g in declared} == {
            "version SSOT",
            "workflow interfaces",
        }
