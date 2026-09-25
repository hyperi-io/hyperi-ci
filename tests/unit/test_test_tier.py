# Project:   HyperI CI
# File:      tests/unit/test_test_tier.py
# Purpose:   Test tier (core | full) resolution, CLI flag and dispatch
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyperi_ci import dispatch
from hyperi_ci.cli import app
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.languages import tiering
from hyperi_ci.languages.golang import test as go_test
from hyperi_ci.languages.tiering import (
    InvalidTestTierError,
    SuiteTier,
    announce_tier,
    handler_tier,
    resolve_test_tier,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Python project with no tier configured and no tier in the env."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    # setenv first, so teardown also removes what `--tier` writes to os.environ.
    monkeypatch.setenv("HYPERCI_TEST_TIER", "core")
    monkeypatch.delenv("HYPERCI_TEST_TIER")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("HYPERCI_AUTO_UPDATE", "false")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_tier(project: Path, tier: str) -> None:
    (project / ".hyperi-ci.yaml").write_text(
        f"test:\n  tier: {tier}\n", encoding="utf-8"
    )


class TestResolution:
    """Cascade: --tier > HYPERCI_TEST_TIER > .hyperi-ci.yaml > defaults."""

    def test_default_is_core(self, project: Path) -> None:
        config = load_config(reload=True, project_dir=project)
        assert resolve_test_tier(config) is SuiteTier.CORE

    def test_project_file_selects_full(self, project: Path) -> None:
        _write_tier(project, "full")
        config = load_config(reload=True, project_dir=project)
        assert resolve_test_tier(config) is SuiteTier.FULL

    def test_env_beats_the_project_file(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_tier(project, "full")
        monkeypatch.setenv("HYPERCI_TEST_TIER", "core")
        config = load_config(reload=True, project_dir=project)
        assert resolve_test_tier(config) is SuiteTier.CORE

    def test_cli_flag_beats_the_env(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_TEST_TIER", "core")
        seen: list[SuiteTier] = []

        def _record(stage: str, **_kwargs: Any) -> int:
            seen.append(resolve_test_tier(load_config(reload=True)))
            return 0

        monkeypatch.setattr("hyperi_ci.cli.run_stage", _record)
        result = CliRunner().invoke(app, ["run", "test", "--tier", "full"])
        assert result.exit_code == 0, result.output
        assert seen == [SuiteTier.FULL]

    def test_case_and_whitespace_are_forgiven(self) -> None:
        config = CIConfig(_raw={"test": {"tier": " FULL "}})
        assert resolve_test_tier(config) is SuiteTier.FULL

    def test_absent_key_is_core(self) -> None:
        assert resolve_test_tier(CIConfig(_raw={})) is SuiteTier.CORE

    @pytest.mark.parametrize("raw", [None, "", "  "])
    def test_empty_value_is_unset_not_invalid(self, raw: object) -> None:
        """A workflow passing an empty output must not fail the stage."""
        config = CIConfig(_raw={"test": {"tier": raw}})
        assert resolve_test_tier(config) is SuiteTier.CORE

    def test_empty_env_var_is_core(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_TEST_TIER", "")
        config = load_config(reload=True, project_dir=project)
        assert resolve_test_tier(config) is SuiteTier.CORE

    def test_rust_tier_is_a_different_key(self) -> None:
        """`test.rust.tier: unit` picks a test kind; it must not read as a tier."""
        config = CIConfig(_raw={"test": {"rust": {"tier": "unit"}}})
        assert resolve_test_tier(config) is SuiteTier.CORE


class TestInvalidValueFailsLoudly:
    """A typo must not quietly run the smaller suite."""

    @pytest.mark.parametrize("raw", ["ful", "all", "unit", True, 1, ["full"]])
    def test_unrecognised_value_raises(self, raw: object) -> None:
        config = CIConfig(_raw={"test": {"tier": raw}})
        with pytest.raises(InvalidTestTierError, match="expected one of core, full"):
            resolve_test_tier(config)

    def test_stage_fails_without_running_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _never(*_a: object, **_k: object) -> int:
            raise AssertionError("the handler must not run")

        monkeypatch.setattr(dispatch, "_dispatch_to_handler", _never)
        config = CIConfig(_raw={"test": {"tier": "everything"}})
        assert dispatch.stage_test("python", config) == 1

    def test_cli_rejects_an_unknown_tier(self, project: Path) -> None:
        result = CliRunner().invoke(app, ["run", "test", "--tier", "nightly"])
        assert result.exit_code == 2

    def test_cli_rejects_tier_on_another_stage(self, project: Path) -> None:
        result = CliRunner().invoke(app, ["run", "quality", "--tier", "full"])
        assert result.exit_code == 1
        assert "--tier applies to the test stage" in result.output


class TestTheTierReachesTheHandler:
    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
        seen: list[dict[str, str]] = []

        def _handler(
            language: str,
            stage: str,
            config: CIConfig,
            extra_env: dict[str, str] | None = None,
        ) -> int:
            seen.append(dict(extra_env or {}))
            return 0

        monkeypatch.setattr(dispatch, "_dispatch_to_handler", _handler)
        return seen

    def test_stage_test_passes_the_resolved_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._capture(monkeypatch)
        config = CIConfig(_raw={"test": {"tier": "full"}})
        assert dispatch.stage_test("python", config) == 0
        assert seen[0][tiering.TEST_TIER_ENV] == "full"

    def test_rust_features_still_travel_beside_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._capture(monkeypatch)
        assert dispatch.stage_test("rust", CIConfig(_raw={})) == 0
        assert seen[0] == {"TEST_TIER": "core", "RUST_FEATURES": "all"}

    def test_check_tier_full_reaches_the_handler(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._capture(monkeypatch)
        monkeypatch.setitem(dispatch._STAGE_HANDLERS, "quality", lambda *_a, **_k: 0)
        result = CliRunner().invoke(app, ["check", "--tier", "full"])
        assert result.exit_code == 0, result.output
        assert [env[tiering.TEST_TIER_ENV] for env in seen] == ["full"]

    def test_handler_tier_defaults_to_core(self) -> None:
        assert handler_tier(None) is SuiteTier.CORE
        assert handler_tier({"RUST_FEATURES": "all"}) is SuiteTier.CORE


class TestConfigOutput:
    def test_config_json_shows_the_tier(self, project: Path) -> None:
        result = CliRunner().invoke(app, ["config", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["test"]["tier"] == "core"


class TestAnnouncement:
    def test_ci_gets_a_notice_annotation(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        announce_tier(SuiteTier.CORE, "5 passed, 2 skipped, 3 deselected")
        out = capsys.readouterr().out
        assert out == (
            "::notice title=test tier core::"
            "tier core: 5 passed, 2 skipped, 3 deselected\n"
        )

    def test_locally_it_is_an_info_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        said: list[str] = []
        monkeypatch.setattr("hyperi_ci.common.info", said.append)
        announce_tier(SuiteTier.FULL, "0 skipped")
        assert said == ["tier full: 0 skipped"]
        assert "::notice" not in capsys.readouterr().out


class TestGoAcceptsTheTier:
    """Go has no ignored-test mechanism, so full runs the core command."""

    @staticmethod
    def _commands(monkeypatch: pytest.MonkeyPatch, tier: str) -> list[list[str]]:
        calls: list[list[str]] = []

        class _Done:
            returncode = 0

        def _run(cmd: list[str], *_a: object, **_k: object) -> _Done:
            calls.append(cmd)
            return _Done()

        monkeypatch.setattr(go_test, "run_cmd", _run)
        config = CIConfig(_raw={"test": {"coverage": False}})
        assert go_test.run(config, extra_env={"TEST_TIER": tier}) == 0
        return calls

    def test_full_runs_the_same_command_as_core(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        core = self._commands(monkeypatch, "core")
        full = self._commands(monkeypatch, "full")
        assert core == full == [["go", "test", "-v", "-race", "./..."]]

    def test_full_warns_that_it_ran_core(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A `Test (full)` that ran the core command must say so in the summary."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        self._commands(monkeypatch, "full")
        assert "::warning title=test tier full::" in capsys.readouterr().out

    def test_core_does_not_warn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        self._commands(monkeypatch, "core")
        assert "::warning" not in capsys.readouterr().out
