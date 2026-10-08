# Project:   HyperI CI
# File:      tests/unit/test_rust_test.py
# Purpose:   Tests for Rust test-runner resolution (nextest vs cargo test)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust.test import (
    _build_test_cmd,
    _note_coverage_runner,
    _resolve_runner,
    _run_coverage,
    run,
)

MODULE = "hyperi_ci.languages.rust.test"


def _make_config(nextest: Any = None, **rest: Any) -> CIConfig:
    rust: dict[str, Any] = dict(rest)
    if nextest is not None:
        rust["nextest"] = nextest
    return CIConfig(_raw={"test": {"rust": rust}})


@pytest.fixture
def _no_nextest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{MODULE}._has_nextest", lambda: False)


@pytest.fixture
def _have_nextest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{MODULE}._has_nextest", lambda: True)


class TestRunnerResolution:
    """Which runner is chosen, and whether the choice is announced."""

    @pytest.mark.usefixtures("_have_nextest")
    def test_auto_takes_nextest_when_present(self) -> None:
        assert _resolve_runner(_make_config()) == "nextest"

    @pytest.mark.usefixtures("_no_nextest")
    def test_auto_degrades_to_cargo_when_absent(self) -> None:
        assert _resolve_runner(_make_config()) == "cargo"

    @pytest.mark.usefixtures("_have_nextest")
    def test_false_pins_cargo_even_with_nextest_installed(self) -> None:
        assert _resolve_runner(_make_config(nextest=False)) == "cargo"

    @pytest.mark.usefixtures("_have_nextest")
    def test_true_uses_nextest(self) -> None:
        assert _resolve_runner(_make_config(nextest=True)) == "nextest"

    @pytest.mark.usefixtures("_no_nextest")
    def test_true_refuses_to_degrade(self) -> None:
        """A repo that requires nextest fails rather than testing something else."""
        assert _resolve_runner(_make_config(nextest=True)) is None

    @pytest.mark.usefixtures("_no_nextest")
    def test_unknown_value_falls_back_to_auto(self) -> None:
        """A typo must not silently become `true` and turn the stage red."""
        assert _resolve_runner(_make_config(nextest="yes-please")) == "cargo"


class TestDegradationIsLoud:
    """The whole point: a silent swap is indistinguishable from a real run."""

    @pytest.mark.usefixtures("_no_nextest")
    def test_annotation_emitted_in_ci(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("hyperi_ci.common.is_github_actions", lambda: True)
        _resolve_runner(_make_config())
        out = capsys.readouterr().out
        assert "::warning title=hyperi-ci test runner degraded::" in out
        assert "cargo-nextest not found" in out
        # One line: a newline would end the workflow command early and the
        # remainder would be parsed as another one.
        assert "\n" not in out.split("::warning", 1)[1].rstrip("\n")

    @pytest.mark.usefixtures("_no_nextest")
    def test_no_annotation_outside_ci(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("hyperi_ci.common.is_github_actions", lambda: False)
        _resolve_runner(_make_config())
        assert "::warning" not in capsys.readouterr().out

    @pytest.mark.usefixtures("_have_nextest")
    def test_no_annotation_when_nothing_degraded(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("hyperi_ci.common.is_github_actions", lambda: True)
        _resolve_runner(_make_config())
        assert "::warning" not in capsys.readouterr().out

    @pytest.mark.usefixtures("_no_nextest")
    def test_deliberate_cargo_choice_is_not_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("hyperi_ci.common.is_github_actions", lambda: True)
        _resolve_runner(_make_config(nextest=False))
        assert "::warning" not in capsys.readouterr().out


class TestStageFailsClosed:
    """`nextest: true` with no nextest is a failed stage, not a green one."""

    @pytest.mark.usefixtures("_no_nextest")
    def test_run_returns_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _never(*args: object, **kwargs: object) -> None:
            raise AssertionError("no test command should run")

        monkeypatch.setattr(f"{MODULE}.run_cmd", _never)
        monkeypatch.setattr(f"{MODULE}.stream_cmd", _never)
        assert run(_make_config(nextest=True)) == 1


class TestCoverageOverridesTheRunner:
    """Coverage tools drive cargo's harness, so they undo a nextest resolution."""

    def test_silent_when_cargo_was_already_the_runner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(f"{MODULE}.warn", said.append)
        _note_coverage_runner("cargo", "cargo-tarpaulin")
        assert said == []

    def test_names_the_tool_and_the_divergence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(f"{MODULE}.warn", said.append)
        _note_coverage_runner("nextest", "cargo-tarpaulin")
        assert len(said) == 1
        assert "cargo-tarpaulin" in said[0]
        assert "not nextest" in said[0]


class TestCommandConstruction:
    """The resolved runner, not the host, decides the command."""

    def test_nextest_command(self) -> None:
        assert _build_test_cmd("default", runner="nextest")[:3] == [
            "cargo",
            "nextest",
            "run",
        ]

    def test_cargo_command(self) -> None:
        assert _build_test_cmd("default", runner="cargo")[:2] == ["cargo", "test"]

    def test_all_features(self) -> None:
        assert "--all-features" in _build_test_cmd("all", runner="nextest")

    def test_named_features(self) -> None:
        cmd = _build_test_cmd("kafka|tls", runner="cargo")
        assert cmd[-2:] == ["--features", "kafka|tls"]

    @pytest.mark.parametrize("tier", ["integration", "e2e"])
    def test_serial_flag_matches_the_runner(self, tier: str) -> None:
        """The two spell single-threaded differently; the wrong spelling is a hang
        or a parse error, not a slow run."""
        assert _build_test_cmd("default", rust_tier=tier, runner="nextest")[-2:] == [
            "--jobs",
            "1",
        ]
        assert _build_test_cmd("default", rust_tier=tier, runner="cargo")[-2:] == [
            "--",
            "--test-threads=1",
        ]

    def test_unit_tier_is_lib_only(self) -> None:
        cmd = _build_test_cmd("default", rust_tier="unit", runner="nextest")
        assert cmd[-1] == "--lib"


class TestCoverageKeepsTheResolvedRunner:
    """llvm-cov was chosen over tarpaulin BECAUSE it composes with nextest.
    If the composition is not wired, the choice bought nothing (issue #140)."""

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> list[list[str]]:
        calls: list[list[str]] = []
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            f"{MODULE}.shutil.which",
            lambda tool: None if tool == "cargo-tarpaulin" else "/usr/bin/x",
        )
        monkeypatch.setattr(
            f"{MODULE}.stream_cmd",
            lambda cmd, *a, **k: calls.append(cmd) or (0, ""),
        )
        monkeypatch.setattr(
            f"{MODULE}.run_cmd", lambda *a, **k: MagicMock(returncode=0)
        )
        monkeypatch.setattr(f"{MODULE}.announce_tier", lambda *_a: None)
        return calls

    def test_nextest_is_composed_not_replaced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        calls = self._capture(monkeypatch, tmp_path)
        _run_coverage("default", runner="nextest")
        assert calls[0][:3] == ["cargo", "llvm-cov", "nextest"]

    def test_cargo_runner_does_not_get_a_nextest_arg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        calls = self._capture(monkeypatch, tmp_path)
        _run_coverage("default", runner="cargo")
        assert "nextest" not in calls[0]

    def test_llvm_cov_no_longer_warns_about_a_divergence_it_does_not_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(f"{MODULE}.warn", said.append)
        _note_coverage_runner("nextest", "cargo-llvm-cov")
        assert said == []

    def test_tarpaulin_still_warns_because_it_still_swaps_the_harness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(f"{MODULE}.warn", said.append)
        _note_coverage_runner("nextest", "cargo-tarpaulin")
        assert len(said) == 1


class TestCoverageSaysWhenItDidNotRun:
    """No runner image carries a coverage tool, so this is the live path."""

    def test_in_ci_it_annotates_rather_than_logs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(f"{MODULE}.shutil.which", lambda _: None)
        monkeypatch.setattr("hyperi_ci.common.is_github_actions", lambda: True)
        assert _run_coverage("default") == -1
        out = capsys.readouterr().out
        assert "::warning title=hyperi-ci coverage skipped::" in out
        assert "did NOT run" in out

    def test_locally_there_is_no_annotation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(f"{MODULE}.shutil.which", lambda _: None)
        monkeypatch.setattr("hyperi_ci.common.is_github_actions", lambda: False)
        assert _run_coverage("default") == -1
        assert "::warning" not in capsys.readouterr().out


type _Call = tuple[list[str], dict[str, str] | None]

_INCREMENTAL = {"CARGO_INCREMENTAL": "1", "RUSTC_WRAPPER": ""}


class _Recorder:
    """Stands in for stream_cmd and run_cmd, keeping each command and its env."""

    def __init__(self) -> None:
        self.streamed: list[_Call] = []
        self.ran: list[_Call] = []
        self.report_rc = 0

    def stream(
        self, cmd: list[str], *, env: dict[str, str] | None = None, **_kw: Any
    ) -> tuple[int, str]:
        self.streamed.append((cmd, env))
        return 0, ""

    def run(
        self, cmd: list[str], *, env: dict[str, str] | None = None, **_kw: Any
    ) -> subprocess.CompletedProcess[str]:
        self.ran.append((cmd, env))
        return subprocess.CompletedProcess(cmd, self.report_rc)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Recorder:
    monkeypatch.chdir(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(f"{MODULE}.stream_cmd", rec.stream)
    monkeypatch.setattr(f"{MODULE}.run_cmd", rec.run)
    monkeypatch.setattr(f"{MODULE}.announce_tier", lambda *_a: None)
    monkeypatch.setattr(f"{MODULE}._has_nextest", lambda: True)
    return rec


def _coverage_tool(monkeypatch: pytest.MonkeyPatch, tool: str | None) -> None:
    monkeypatch.setattr(
        f"{MODULE}.shutil.which", lambda name: "/usr/bin/x" if name == tool else None
    )


class TestLlvmCovRunsIncremental:
    """The ARC runner turns incremental off, which makes llvm-cov report
    "mismatched data" for every cross-crate-inlinable function."""

    def test_the_coverage_run_turns_incremental_back_on(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        assert _run_coverage("default", runner="nextest") == 0
        assert recorder.streamed[0][0][:2] == ["cargo", "llvm-cov"]
        assert recorder.streamed[0][1] == _INCREMENTAL

    def test_the_coverage_run_bypasses_sccache(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        """sccache refuses CARGO_INCREMENTAL=1, so the wrapper is emptied (#376)."""
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        _run_coverage("default", runner="cargo")
        env = recorder.streamed[0][1]
        assert env is not None
        assert env["RUSTC_WRAPPER"] == ""

    def test_the_html_report_goes_through_run_cmd_with_the_same_env(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        _run_coverage("default", runner="cargo")
        assert recorder.ran == [
            (
                [
                    "cargo",
                    "llvm-cov",
                    "report",
                    "--html",
                    "--output-dir",
                    "test-results/coverage-html",
                ],
                _INCREMENTAL,
            )
        ]

    def test_a_failed_report_is_not_announced_as_written(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        recorder.report_rc = 1
        said: list[str] = []
        told: list[str] = []
        monkeypatch.setattr(f"{MODULE}.warn", said.append)
        monkeypatch.setattr(f"{MODULE}.info", told.append)
        assert _run_coverage("default", runner="cargo") == 0
        assert any("no HTML report" in line for line in said)
        assert not any("Coverage report:" in line for line in told)

    def test_tarpaulin_keeps_the_inherited_env(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        _coverage_tool(monkeypatch, "cargo-tarpaulin")
        _run_coverage("default", runner="cargo")
        assert recorder.streamed[0][0][:2] == ["cargo", "tarpaulin"]
        assert recorder.streamed[0][1] is None

    def test_a_plain_run_keeps_the_inherited_env(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        config = CIConfig(_raw={"test": {"coverage": False}})
        assert run(config, extra_env={"RUST_FEATURES": "default"}) == 0
        assert recorder.streamed[0][0][:3] == ["cargo", "nextest", "run"]
        assert recorder.streamed[0][1] is None


class TestHtmlReportCoversEveryWorkspaceMember:
    """`cargo llvm-cov report` has no --workspace, so the HTML report names
    every member with -p, matching what the --workspace run already covers
    in lcov.info (issue #432)."""

    _PACKAGES = {"packages": [{"name": "root_pkg"}, {"name": "member_a"}]}

    def test_html_report_lists_every_member_with_dash_p(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        monkeypatch.setattr(f"{MODULE}.cargo_metadata", lambda: self._PACKAGES)
        assert _run_coverage("default", runner="cargo", workspace=True) == 0
        assert recorder.ran == [
            (
                [
                    "cargo",
                    "llvm-cov",
                    "report",
                    "--html",
                    "--output-dir",
                    "test-results/coverage-html",
                    "-p",
                    "root_pkg",
                    "-p",
                    "member_a",
                ],
                _INCREMENTAL,
            )
        ]

    def test_html_report_and_the_lcov_run_cover_the_same_packages(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        """The run that writes lcov.info takes --workspace; the HTML report,
        which cannot, names the same members explicitly with -p."""
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        monkeypatch.setattr(f"{MODULE}.cargo_metadata", lambda: self._PACKAGES)
        assert _run_coverage("default", runner="cargo", workspace=True) == 0
        lcov_cmd = recorder.streamed[0][0]
        html_cmd = recorder.ran[0][0]
        assert "--workspace" in lcov_cmd
        assert html_cmd[html_cmd.index("-p") :] == ["-p", "root_pkg", "-p", "member_a"]

    def test_outside_a_workspace_no_dash_p_is_added(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        asked: list[None] = []
        monkeypatch.setattr(
            f"{MODULE}.cargo_metadata", lambda: asked.append(None) or self._PACKAGES
        )
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        assert _run_coverage("default", runner="cargo", workspace=False) == 0
        assert recorder.ran == [
            (
                [
                    "cargo",
                    "llvm-cov",
                    "report",
                    "--html",
                    "--output-dir",
                    "test-results/coverage-html",
                ],
                _INCREMENTAL,
            )
        ]
        assert not asked

    def test_metadata_failure_falls_back_to_no_dash_p(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        monkeypatch.setattr(f"{MODULE}.cargo_metadata", lambda: None)
        said: list[str] = []
        monkeypatch.setattr(f"{MODULE}.warn", said.append)
        assert _run_coverage("default", runner="cargo", workspace=True) == 0
        assert recorder.ran[0][0] == [
            "cargo",
            "llvm-cov",
            "report",
            "--html",
            "--output-dir",
            "test-results/coverage-html",
        ]
        assert any("cargo metadata failed" in line for line in said)


class TestCoverageOnTheFirstFeatureSetOnly:
    """Every coverage run writes the same lcov.info, so only one can be kept."""

    def test_later_feature_sets_run_plain(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        _coverage_tool(monkeypatch, "cargo-llvm-cov")
        assert run(_make_config(), extra_env={"RUST_FEATURES": "a|b|c"}) == 0
        commands = [cmd for cmd, _env in recorder.streamed]
        assert commands[0][:3] == ["cargo", "llvm-cov", "nextest"]
        assert commands[0][-2:] == ["--features", "a"]
        assert commands[1:] == [
            ["cargo", "nextest", "run", "--features", "b"],
            ["cargo", "nextest", "run", "--features", "c"],
        ]
        assert len(recorder.ran) == 1

    def test_a_missing_tool_is_reported_once(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        _coverage_tool(monkeypatch, None)
        monkeypatch.setattr("hyperi_ci.common.is_github_actions", lambda: False)
        said: list[str] = []
        monkeypatch.setattr("hyperi_ci.common.warn", said.append)
        assert run(_make_config(), extra_env={"RUST_FEATURES": "a|b"}) == 0
        assert sum("did NOT run" in line for line in said) == 1
        assert [cmd for cmd, _env in recorder.streamed] == [
            ["cargo", "nextest", "run", "--features", "a"],
            ["cargo", "nextest", "run", "--features", "b"],
        ]
