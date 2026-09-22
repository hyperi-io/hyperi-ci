# Project:   HyperI CI
# File:      tests/unit/test_rust_test.py
# Purpose:   Tests for Rust test-runner resolution (nextest vs cargo test)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from typing import Any

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
        monkeypatch.setattr(f"{MODULE}.is_ci", lambda: True)
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
        monkeypatch.setattr(f"{MODULE}.is_ci", lambda: False)
        _resolve_runner(_make_config())
        assert "::warning" not in capsys.readouterr().out

    @pytest.mark.usefixtures("_have_nextest")
    def test_no_annotation_when_nothing_degraded(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(f"{MODULE}.is_ci", lambda: True)
        _resolve_runner(_make_config())
        assert "::warning" not in capsys.readouterr().out

    @pytest.mark.usefixtures("_no_nextest")
    def test_deliberate_cargo_choice_is_not_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(f"{MODULE}.is_ci", lambda: True)
        _resolve_runner(_make_config(nextest=False))
        assert "::warning" not in capsys.readouterr().out


class TestStageFailsClosed:
    """`nextest: true` with no nextest is a failed stage, not a green one."""

    @pytest.mark.usefixtures("_no_nextest")
    def test_run_returns_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _never(*args: object, **kwargs: object) -> None:
            raise AssertionError("no test command should run")

        monkeypatch.setattr(f"{MODULE}.subprocess.run", _never)
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
        assert _build_test_cmd("default", tier=tier, runner="nextest")[-2:] == [
            "--jobs",
            "1",
        ]
        assert _build_test_cmd("default", tier=tier, runner="cargo")[-2:] == [
            "--",
            "--test-threads=1",
        ]

    def test_unit_tier_is_lib_only(self) -> None:
        assert _build_test_cmd("default", tier="unit", runner="nextest")[-1] == "--lib"


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
        monkeypatch.setattr(f"{MODULE}.is_ci", lambda: True)
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
        monkeypatch.setattr(f"{MODULE}.is_ci", lambda: False)
        assert _run_coverage("default") == -1
        assert "::warning" not in capsys.readouterr().out
