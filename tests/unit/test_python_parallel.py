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
        version = parallel.xdist_version([sys.executable, "-m", "pytest"])
        assert version is not None
        assert version >= parallel._WORKSTEAL_MIN_XDIST

    def test_the_version_is_read_from_the_plugin_line(self) -> None:
        line = "  pytest-xdist-3.1.4 at /x/xdist/plugin.py"
        cmd = [sys.executable, "-c", f"print({line!r})"]
        assert parallel.xdist_version(cmd) == (3, 1)

    def test_an_unreadable_version_is_present_but_unknown(self) -> None:
        cmd = [sys.executable, "-c", "print('pytest-xdist at /x/plugin.py')"]
        assert parallel.xdist_version(cmd) == (0, 0)

    def test_a_command_without_xdist_reads_as_absent(self) -> None:
        assert (
            parallel.xdist_version([sys.executable, "-c", "print('no plugins here')"])
            is None
        )

    def test_a_command_that_does_not_exist_reads_as_absent(self) -> None:
        assert parallel.xdist_version(["hyperi-ci-no-such-binary-9f2c"]) is None

    def test_a_failing_command_reads_as_absent(self) -> None:
        assert (
            parallel.xdist_version([sys.executable, "-c", "raise SystemExit(3)"])
            is None
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
        monkeypatch.setattr(parallel, "xdist_version", lambda _cmd: (3, 8))
        monkeypatch.setattr(parallel, "cpu_budget", lambda: 8)
        assert parallel.parallel_args(_config(True), ["-v"], ["pytest"]) == [
            "-n",
            "8",
            "--dist",
            "worksteal",
        ]

    def test_a_project_with_its_own_n_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(parallel, "xdist_version", lambda _cmd: (3, 8))
        assert parallel.parallel_args(_config(True), ["-n", "4"], ["pytest"]) == []

    def test_a_missing_xdist_warns_and_runs_serial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project without the plugin must be unaffected, not broken."""
        warnings: list[str] = []
        monkeypatch.setattr(parallel, "warn", warnings.append)
        monkeypatch.setattr(parallel, "xdist_version", lambda _cmd: None)
        assert parallel.parallel_args(_config(True), ["-v"], ["pytest"]) == []
        assert any("pytest-xdist" in m for m in warnings)

    def test_the_plugin_is_not_probed_when_serial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No subprocess cost for the four-in-five projects that opt out."""

        def _never(_cmd: list[str]) -> tuple[int, int] | None:
            raise AssertionError("probed pytest for a serial run")

        monkeypatch.setattr(parallel, "xdist_version", _never)
        assert parallel.parallel_args(_config(False), ["-v"], ["pytest"]) == []


class TestProjectSetsOwnDist:
    def test_a_bare_repo_claims_nothing(self, tmp_path: Path) -> None:
        assert not parallel.project_sets_own_dist(["-v", "-n", "4"], tmp_path)

    def test_a_separated_dist_in_our_own_args_counts(self, tmp_path: Path) -> None:
        assert parallel.project_sets_own_dist(["--dist", "loadgroup"], tmp_path)

    def test_an_attached_dist_in_our_own_args_counts(self, tmp_path: Path) -> None:
        assert parallel.project_sets_own_dist(["--dist=loadscope"], tmp_path)

    def test_the_load_shorthand_counts(self, tmp_path: Path) -> None:
        """xdist's ``-d`` overrides ``--dist``, so appending one would be ignored."""
        assert parallel.project_sets_own_dist(["-d"], tmp_path)

    def test_a_distinct_long_flag_is_not_dist(self, tmp_path: Path) -> None:
        assert not parallel.project_sets_own_dist(["--distance=3"], tmp_path)

    def test_pyproject_addopts_count(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = "-n 4 --dist loadfile"\n',
            encoding="utf-8",
            newline="\n",
        )
        assert parallel.project_sets_own_dist([], tmp_path)

    def test_pyproject_addopts_as_a_list_count(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = ["--dist=loadgroup"]\n',
            encoding="utf-8",
            newline="\n",
        )
        assert parallel.project_sets_own_dist([], tmp_path)

    def test_pytest_ini_addopts_count(self, tmp_path: Path) -> None:
        (tmp_path / "pytest.ini").write_text(
            "[pytest]\naddopts = --dist load\n", encoding="utf-8", newline="\n"
        )
        assert parallel.project_sets_own_dist([], tmp_path)

    def test_setup_cfg_addopts_count(self, tmp_path: Path) -> None:
        (tmp_path / "setup.cfg").write_text(
            "[tool:pytest]\naddopts = --dist=each\n", encoding="utf-8", newline="\n"
        )
        assert parallel.project_sets_own_dist([], tmp_path)

    def test_tox_ini_addopts_count(self, tmp_path: Path) -> None:
        (tmp_path / "tox.ini").write_text(
            "[pytest]\naddopts = --dist loadscope\n", encoding="utf-8", newline="\n"
        )
        assert parallel.project_sets_own_dist([], tmp_path)

    def test_pytest_addopts_env_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "--dist loadgroup")
        assert parallel.project_sets_own_dist([], tmp_path)


class TestWorksteal:
    """``--dist worksteal`` rides only on a ``-n`` that hyperi-ci supplied."""

    @pytest.fixture(autouse=True)
    def _in_a_bare_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(parallel, "cpu_budget", lambda: 4)
        monkeypatch.setattr(parallel, "xdist_version", lambda _cmd: (3, 8))

    def test_an_auto_n_brings_worksteal(self) -> None:
        args = parallel.parallel_args(_config("auto"), ["-v"], ["pytest"])
        assert args == ["-n", "4", "--dist", "worksteal"]

    def test_an_explicit_count_brings_worksteal(self) -> None:
        args = parallel.parallel_args(_config(3), ["-v"], ["pytest"])
        assert args == ["-n", "3", "--dist", "worksteal"]

    def test_our_own_args_setting_dist_keep_their_mode(self) -> None:
        args = parallel.parallel_args(
            _config(True), ["-v", "--dist", "loadgroup"], ["pytest"]
        )
        assert args == ["-n", "4"]

    def test_pyproject_addopts_setting_dist_keep_their_mode(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = "--dist loadgroup"\n',
            encoding="utf-8",
            newline="\n",
        )
        args = parallel.parallel_args(_config(True), ["-v"], ["pytest"])
        assert args == ["-n", "4"]

    def test_pytest_addopts_env_setting_dist_keeps_its_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "--dist=loadfile")
        args = parallel.parallel_args(_config(True), ["-v"], ["pytest"])
        assert args == ["-n", "4"]

    def test_a_project_n_gets_no_dist_either(self) -> None:
        """The project owns the whole xdist decision once it sets ``-n``."""
        assert parallel.parallel_args(_config(True), ["-n", "2"], ["pytest"]) == []

    def test_a_pyproject_n_gets_no_dist_either(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = "-n 2"\n',
            encoding="utf-8",
            newline="\n",
        )
        assert parallel.parallel_args(_config(True), ["-v"], ["pytest"]) == []

    def test_a_serial_run_gets_no_dist(self) -> None:
        assert parallel.parallel_args(_config(False), ["-v"], ["pytest"]) == []

    def test_an_xdist_without_worksteal_keeps_the_default_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """xdist before 3.2 rejects ``--dist worksteal`` and runs nothing."""
        warnings: list[str] = []
        monkeypatch.setattr(parallel, "warn", warnings.append)
        monkeypatch.setattr(parallel, "xdist_version", lambda _cmd: (3, 1))
        args = parallel.parallel_args(_config(True), ["-v"], ["pytest"])
        assert args == ["-n", "4"]
        assert any("3.2" in m and "3.1" in m for m in warnings)

    def test_an_unreadable_xdist_version_keeps_the_default_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(parallel, "warn", warnings.append)
        monkeypatch.setattr(parallel, "xdist_version", lambda _cmd: (0, 0))
        args = parallel.parallel_args(_config(True), ["-v"], ["pytest"])
        assert args == ["-n", "4"]
        assert any("unknown" in m for m in warnings)


class TestNativeTomlConfig:
    """pytest 9 also reads ``pytest.toml``, ``.pytest.toml`` and ``[tool.pytest]``."""

    @staticmethod
    def _write(root: Path, filename: str, body: str) -> None:
        (root / filename).write_text(body, encoding="utf-8", newline="\n")

    @pytest.mark.parametrize("filename", ["pytest.toml", ".pytest.toml"])
    def test_a_pytest_toml_n_counts(self, tmp_path: Path, filename: str) -> None:
        self._write(tmp_path, filename, '[pytest]\naddopts = ["-n", "4"]\n')
        assert parallel.project_sets_own_workers([], tmp_path)

    @pytest.mark.parametrize("filename", ["pytest.toml", ".pytest.toml"])
    def test_a_pytest_toml_dist_counts(self, tmp_path: Path, filename: str) -> None:
        self._write(tmp_path, filename, '[pytest]\naddopts = "--dist loadgroup"\n')
        assert parallel.project_sets_own_dist([], tmp_path)

    def test_a_native_tool_pytest_n_counts(self, tmp_path: Path) -> None:
        self._write(
            tmp_path, "pyproject.toml", '[tool.pytest]\naddopts = ["-n", "2"]\n'
        )
        assert parallel.project_sets_own_workers([], tmp_path)

    def test_a_native_tool_pytest_dist_counts(self, tmp_path: Path) -> None:
        self._write(
            tmp_path, "pyproject.toml", '[tool.pytest]\naddopts = ["--dist=loadfile"]\n'
        )
        assert parallel.project_sets_own_dist([], tmp_path)

    def test_a_pytest_toml_without_addopts_claims_nothing(self, tmp_path: Path) -> None:
        self._write(tmp_path, "pytest.toml", '[pytest]\nminversion = "9.0"\n')
        assert not parallel.project_sets_own_workers([], tmp_path)
        assert not parallel.project_sets_own_dist([], tmp_path)

    def test_an_odd_tool_value_does_not_blow_up(self, tmp_path: Path) -> None:
        self._write(tmp_path, "pyproject.toml", "tool = 1\n")
        assert not parallel.project_sets_own_workers([], tmp_path)

    def test_a_pytest_toml_dist_keeps_the_projects_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write(tmp_path, "pytest.toml", '[pytest]\naddopts = ["--dist", "load"]\n')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(parallel, "cpu_budget", lambda: 4)
        monkeypatch.setattr(parallel, "xdist_version", lambda _cmd: (3, 8))
        assert parallel.parallel_args(_config(True), ["-v"], ["pytest"]) == ["-n", "4"]
