# Project:   HyperI CI
# File:      tests/unit/test_python_parallel.py
# Purpose:   Tests for pytest worker-count resolution and the hands-off rules
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.languages.python.parallel`.

Real project files in ``tmp_path``: the detection that matters reads the
``addopts`` a project has actually written, so a fabricated one proves
nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.python import parallel


def _config(value: object) -> CIConfig:
    """Config carrying one ``test.python.parallel`` setting."""
    return CIConfig(_raw={"test": {"python": {"parallel": value}}})


@pytest.fixture(autouse=True)
def _no_ambient_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own environment out of every case."""
    monkeypatch.delenv("HYPERCI_TEST_WORKERS", raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)


class TestRequestedWorkers:
    def test_the_default_is_serial(self) -> None:
        assert parallel.requested_workers(CIConfig(_raw={})) is None

    def test_false_is_serial(self) -> None:
        assert parallel.requested_workers(_config(False)) is None

    def test_true_derives_from_the_host(self) -> None:
        assert parallel.requested_workers(_config(True)) == parallel.auto_workers()

    def test_auto_derives_from_the_host(self) -> None:
        assert parallel.requested_workers(_config("auto")) == parallel.auto_workers()

    def test_an_explicit_count_is_honoured(self) -> None:
        assert parallel.requested_workers(_config(3)) == 3

    def test_a_quoted_count_is_honoured(self) -> None:
        assert parallel.requested_workers(_config("6")) == 6

    def test_zero_is_serial(self) -> None:
        assert parallel.requested_workers(_config(0)) is None

    def test_an_unknown_value_warns_and_runs_serial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo must not silently pick a number of its own."""
        warnings: list[str] = []
        monkeypatch.setattr(parallel, "warn", warnings.append)
        assert parallel.requested_workers(_config("yes please")) is None
        assert any("test.python.parallel" in m for m in warnings)


class TestAutoWorkers:
    def test_it_never_exceeds_the_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(parallel, "cpu_budget", lambda: 96)
        assert parallel.auto_workers() == parallel._MAX_AUTO_WORKERS

    def test_it_tracks_a_small_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(parallel, "cpu_budget", lambda: 4)
        assert parallel.auto_workers() == 4

    def test_it_never_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(parallel, "cpu_budget", lambda: 0)
        assert parallel.auto_workers() == 1


class TestEnvOverride:
    def test_a_count_beats_the_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYPERCI_TEST_WORKERS", "5")
        assert parallel.requested_workers(_config(False)) == 5

    def test_zero_forces_serial_over_an_opt_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_TEST_WORKERS", "0")
        assert parallel.requested_workers(_config(True)) is None

    def test_junk_is_ignored_rather_than_obeyed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(parallel, "warn", warnings.append)
        monkeypatch.setenv("HYPERCI_TEST_WORKERS", "lots")
        assert parallel.requested_workers(_config(False)) is None
        assert any("HYPERCI_TEST_WORKERS" in m for m in warnings)


class TestProjectSetsOwnWorkers:
    def test_a_bare_repo_claims_nothing(self, tmp_path: Path) -> None:
        assert not parallel.project_sets_own_workers([], tmp_path)

    def test_a_separated_n_in_our_own_args_counts(self, tmp_path: Path) -> None:
        assert parallel.project_sets_own_workers(["-v", "-n", "4"], tmp_path)

    def test_an_attached_n_in_our_own_args_counts(self, tmp_path: Path) -> None:
        assert parallel.project_sets_own_workers(["-n4"], tmp_path)

    def test_the_long_form_counts(self, tmp_path: Path) -> None:
        assert parallel.project_sets_own_workers(["--numprocesses=auto"], tmp_path)

    def test_a_lone_dash_n_is_not_a_worker_count(self, tmp_path: Path) -> None:
        """``-no-header`` is not pytest, but a stray short flag must not trip it."""
        assert not parallel.project_sets_own_workers(["-q", "--tb=short"], tmp_path)

    def test_pyproject_addopts_count(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = "-vv -n 4 --cov=src"\n',
            encoding="utf-8",
            newline="\n",
        )
        assert parallel.project_sets_own_workers([], tmp_path)

    def test_pyproject_addopts_as_a_list_count(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = ["-vv", "-n", "4"]\n',
            encoding="utf-8",
            newline="\n",
        )
        assert parallel.project_sets_own_workers([], tmp_path)

    def test_pyproject_addopts_without_n_do_not_count(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = \"-m 'not slow'\"\n",
            encoding="utf-8",
            newline="\n",
        )
        assert not parallel.project_sets_own_workers([], tmp_path)

    def test_a_broken_pyproject_does_not_blow_up(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "this is not toml [[[", encoding="utf-8", newline="\n"
        )
        assert not parallel.project_sets_own_workers([], tmp_path)

    def test_pytest_ini_addopts_count(self, tmp_path: Path) -> None:
        (tmp_path / "pytest.ini").write_text(
            "[pytest]\naddopts = -n 8\n", encoding="utf-8", newline="\n"
        )
        assert parallel.project_sets_own_workers([], tmp_path)

    def test_setup_cfg_addopts_count(self, tmp_path: Path) -> None:
        (tmp_path / "setup.cfg").write_text(
            "[tool:pytest]\naddopts = --numprocesses 2\n",
            encoding="utf-8",
            newline="\n",
        )
        assert parallel.project_sets_own_workers([], tmp_path)

    def test_tox_ini_addopts_count(self, tmp_path: Path) -> None:
        (tmp_path / "tox.ini").write_text(
            "[pytest]\naddopts = -n auto\n", encoding="utf-8", newline="\n"
        )
        assert parallel.project_sets_own_workers([], tmp_path)

    def test_a_disabled_xdist_plugin_is_hands_off(self, tmp_path: Path) -> None:
        """Turning the plugin off is a parallelism decision too."""
        assert parallel.project_sets_own_workers(["-p", "no:xdist"], tmp_path)

    def test_an_attached_plugin_disable_is_hands_off(self, tmp_path: Path) -> None:
        assert parallel.project_sets_own_workers(["-pno:xdist"], tmp_path)

    def test_pytest_addopts_env_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "-n 2")
        assert parallel.project_sets_own_workers([], tmp_path)


class TestXdistProbe:
    def test_the_project_pytest_reports_its_plugins(self) -> None:
        """A real probe of a real pytest - this venv has xdist installed."""
        assert parallel.xdist_installed([sys.executable, "-m", "pytest"])

    def test_a_command_without_xdist_reads_as_absent(self) -> None:
        assert not parallel.xdist_installed(
            [sys.executable, "-c", "print('no plugins here')"]
        )

    def test_a_command_that_does_not_exist_reads_as_absent(self) -> None:
        assert not parallel.xdist_installed(["hyperi-ci-no-such-binary-9f2c"])

    def test_a_failing_command_reads_as_absent(self) -> None:
        assert not parallel.xdist_installed(
            [sys.executable, "-c", "raise SystemExit(3)"]
        )


class TestParallelArgs:
    @pytest.fixture(autouse=True)
    def _in_a_bare_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

    def test_serial_by_default_adds_nothing(self) -> None:
        assert parallel.parallel_args(CIConfig(_raw={}), ["-v"], ["pytest"]) == []

    def test_an_opt_in_adds_the_worker_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(parallel, "xdist_installed", lambda _cmd: True)
        monkeypatch.setattr(parallel, "cpu_budget", lambda: 8)
        assert parallel.parallel_args(_config(True), ["-v"], ["pytest"]) == ["-n", "8"]

    def test_a_project_with_its_own_n_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(parallel, "xdist_installed", lambda _cmd: True)
        assert parallel.parallel_args(_config(True), ["-n", "4"], ["pytest"]) == []

    def test_a_missing_xdist_warns_and_runs_serial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project without the plugin must be unaffected, not broken."""
        warnings: list[str] = []
        monkeypatch.setattr(parallel, "warn", warnings.append)
        monkeypatch.setattr(parallel, "xdist_installed", lambda _cmd: False)
        assert parallel.parallel_args(_config(True), ["-v"], ["pytest"]) == []
        assert any("pytest-xdist" in m for m in warnings)

    def test_the_plugin_is_not_probed_when_serial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No subprocess cost for the four-in-five projects that opt out."""

        def _never(_cmd: list[str]) -> bool:
            raise AssertionError("probed pytest for a serial run")

        monkeypatch.setattr(parallel, "xdist_installed", _never)
        assert parallel.parallel_args(_config(False), ["-v"], ["pytest"]) == []
