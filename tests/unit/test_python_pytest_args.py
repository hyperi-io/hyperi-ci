# Project:   HyperI CI
# File:      tests/unit/test_python_pytest_args.py
# Purpose:   Reading the pytest arguments a project already passes itself
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path

import pytest

from hyperi_ci.languages.python.pytest_args import (
    config_file_addopts,
    option_values,
    project_args,
)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


class TestConfigFileAddopts:
    def test_no_config_file_means_no_addopts(self, tmp_path: Path) -> None:
        assert config_file_addopts(tmp_path) == []

    def test_pyproject_ini_options_string(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "pyproject.toml",
            "[tool.pytest.ini_options]\naddopts = \"-ra -m 'not slow'\"\n",
        )
        assert config_file_addopts(tmp_path) == ["-ra", "-m", "not slow"]

    def test_pyproject_ini_options_list(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = ["-n", "4"]\n',
        )
        assert config_file_addopts(tmp_path) == ["-n", "4"]

    def test_pyproject_native_table(self, tmp_path: Path) -> None:
        """pytest 9 reads ``[tool.pytest]`` with native TOML types."""
        _write(
            tmp_path / "pyproject.toml",
            '[tool.pytest]\naddopts = ["--durations=5"]\n',
        )
        assert config_file_addopts(tmp_path) == ["--durations=5"]

    def test_pytest_toml(self, tmp_path: Path) -> None:
        _write(tmp_path / "pytest.toml", '[pytest]\naddopts = ["-rA"]\n')
        assert config_file_addopts(tmp_path) == ["-rA"]

    def test_hidden_pytest_toml(self, tmp_path: Path) -> None:
        _write(tmp_path / ".pytest.toml", '[pytest]\naddopts = ["-rA"]\n')
        assert config_file_addopts(tmp_path) == ["-rA"]

    def test_pytest_ini(self, tmp_path: Path) -> None:
        _write(tmp_path / "pytest.ini", "[pytest]\naddopts = -n 8\n")
        assert config_file_addopts(tmp_path) == ["-n", "8"]

    def test_hidden_pytest_ini(self, tmp_path: Path) -> None:
        _write(tmp_path / ".pytest.ini", "[pytest]\naddopts = -n 8\n")
        assert config_file_addopts(tmp_path) == ["-n", "8"]

    def test_tox_ini(self, tmp_path: Path) -> None:
        _write(tmp_path / "tox.ini", "[pytest]\naddopts = --durations 10\n")
        assert config_file_addopts(tmp_path) == ["--durations", "10"]

    def test_setup_cfg(self, tmp_path: Path) -> None:
        _write(tmp_path / "setup.cfg", "[tool:pytest]\naddopts = -rs\n")
        assert config_file_addopts(tmp_path) == ["-rs"]

    def test_a_percent_sign_is_not_interpolation(self, tmp_path: Path) -> None:
        _write(tmp_path / "pytest.ini", "[pytest]\naddopts = -k 'not 100%'\n")
        assert config_file_addopts(tmp_path) == ["-k", "not 100%"]

    def test_multi_line_ini_value(self, tmp_path: Path) -> None:
        _write(tmp_path / "pytest.ini", "[pytest]\naddopts =\n    -v\n    -n 2\n")
        assert config_file_addopts(tmp_path) == ["-v", "-n", "2"]

    def test_only_the_file_pytest_picks_is_read(self, tmp_path: Path) -> None:
        """pytest.ini outranks pyproject.toml even when it has no addopts."""
        _write(tmp_path / "pytest.ini", "[pytest]\ntestpaths = tests\n")
        _write(
            tmp_path / "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = "-n 4"\n',
        )
        assert config_file_addopts(tmp_path) == []

    def test_a_tox_ini_without_pytest_section_is_passed_over(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "tox.ini", "[tox]\nenvlist = py314\n")
        _write(tmp_path / "setup.cfg", "[tool:pytest]\naddopts = -n 3\n")
        assert config_file_addopts(tmp_path) == ["-n", "3"]

    def test_a_pyproject_without_pytest_tables_is_passed_over(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "pyproject.toml", '[project]\nname = "x"\n')
        _write(tmp_path / "tox.ini", "[pytest]\naddopts = -n 3\n")
        assert config_file_addopts(tmp_path) == ["-n", "3"]

    def test_a_broken_pyproject_reads_as_nothing(self, tmp_path: Path) -> None:
        _write(tmp_path / "pyproject.toml", "this is not toml [[[")
        assert config_file_addopts(tmp_path) == []

    def test_an_unbalanced_quote_still_splits(self, tmp_path: Path) -> None:
        _write(tmp_path / "pytest.ini", "[pytest]\naddopts = -n 2 -k 'oops\n")
        assert config_file_addopts(tmp_path)[:2] == ["-n", "2"]


class TestProjectArgs:
    def test_sources_come_in_pytest_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config file, then PYTEST_ADDOPTS, then the command line."""
        _write(tmp_path / "pytest.ini", "[pytest]\naddopts = -ra\n")
        monkeypatch.setenv("PYTEST_ADDOPTS", "-rx")
        assert project_args(["-rf"], tmp_path) == ["-ra", "-rx", "-rf"]

    def test_defaults_to_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path / "pytest.ini", "[pytest]\naddopts = -v\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        assert project_args([]) == ["-v"]


class TestOptionValues:
    def test_separated_value(self) -> None:
        assert option_values(["-r", "a"], "--report-chars", "-r") == ["a"]

    def test_attached_short_value(self) -> None:
        assert option_values(["-rfE"], "--report-chars", "-r") == ["fE"]

    def test_long_with_equals(self) -> None:
        assert option_values(["--durations=10"], "--durations") == ["10"]

    def test_long_separated(self) -> None:
        assert option_values(["--durations", "10"], "--durations") == ["10"]

    def test_every_value_in_order(self) -> None:
        assert option_values(["-ra", "-v", "-rs"], "--report-chars", "-r") == [
            "a",
            "s",
        ]

    def test_a_longer_option_sharing_the_prefix_is_not_it(self) -> None:
        assert option_values(["--durations-min=1.0"], "--durations") == []

    def test_a_trailing_flag_without_value_is_ignored(self) -> None:
        assert option_values(["--durations"], "--durations") == []
