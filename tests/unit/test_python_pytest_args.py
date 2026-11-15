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
    ini_value,
    option_values,
    project_args,
)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


class TestConfigFileAddopts:
    @pytest.mark.parametrize(
        ("case", "files", "expected"),
        [
            ("no-config-file", {}, []),
            (
                "pyproject-ini-options-string",
                {
                    "pyproject.toml": "[tool.pytest.ini_options]\naddopts = \"-ra -m 'not slow'\"\n"
                },
                ["-ra", "-m", "not slow"],
            ),
            (
                "pyproject-ini-options-list",
                {
                    "pyproject.toml": '[tool.pytest.ini_options]\naddopts = ["-n", "4"]\n'
                },
                ["-n", "4"],
            ),
            (
                "pyproject-native-table (pytest 9)",
                {"pyproject.toml": '[tool.pytest]\naddopts = ["--durations=5"]\n'},
                ["--durations=5"],
            ),
            ("pytest-toml", {"pytest.toml": '[pytest]\naddopts = ["-rA"]\n'}, ["-rA"]),
            (
                "hidden-pytest-toml",
                {".pytest.toml": '[pytest]\naddopts = ["-rA"]\n'},
                ["-rA"],
            ),
            ("pytest-ini", {"pytest.ini": "[pytest]\naddopts = -n 8\n"}, ["-n", "8"]),
            (
                "hidden-pytest-ini",
                {".pytest.ini": "[pytest]\naddopts = -n 8\n"},
                ["-n", "8"],
            ),
            (
                "tox-ini",
                {"tox.ini": "[pytest]\naddopts = --durations 10\n"},
                ["--durations", "10"],
            ),
            ("setup-cfg", {"setup.cfg": "[tool:pytest]\naddopts = -rs\n"}, ["-rs"]),
            (
                "a-percent-sign-is-not-interpolation",
                {"pytest.ini": "[pytest]\naddopts = -k 'not 100%'\n"},
                ["-k", "not 100%"],
            ),
            (
                "multi-line-ini-value",
                {"pytest.ini": "[pytest]\naddopts =\n    -v\n    -n 2\n"},
                ["-v", "-n", "2"],
            ),
            (
                "pytest-ini-outranks-pyproject-even-without-addopts",
                {
                    "pytest.ini": "[pytest]\ntestpaths = tests\n",
                    "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-n 4"\n',
                },
                [],
            ),
            (
                "a-tox-ini-without-a-pytest-section-is-passed-over",
                {
                    "tox.ini": "[tox]\nenvlist = py314\n",
                    "setup.cfg": "[tool:pytest]\naddopts = -n 3\n",
                },
                ["-n", "3"],
            ),
            (
                "a-pyproject-without-pytest-tables-is-passed-over",
                {
                    "pyproject.toml": '[project]\nname = "x"\n',
                    "tox.ini": "[pytest]\naddopts = -n 3\n",
                },
                ["-n", "3"],
            ),
            (
                "a-broken-pyproject-reads-as-nothing",
                {"pyproject.toml": "this is not toml [[["},
                [],
            ),
        ],
    )
    def test_the_addopts_read(
        self, tmp_path: Path, case: str, files: dict[str, str], expected: list[str]
    ) -> None:
        for name, text in files.items():
            _write(tmp_path / name, text)
        assert config_file_addopts(tmp_path) == expected

    def test_an_unbalanced_quote_still_splits(self, tmp_path: Path) -> None:
        _write(tmp_path / "pytest.ini", "[pytest]\naddopts = -n 2 -k 'oops\n")
        assert config_file_addopts(tmp_path)[:2] == ["-n", "2"]


class TestIniValue:
    @pytest.mark.parametrize(
        ("case", "files", "expected"),
        [
            ("no-config-file", {}, None),
            (
                "pytest-ini-key",
                {"pytest.ini": "[pytest]\ntmp_path_retention_policy = all\n"},
                "all",
            ),
            (
                "pyproject-ini-options-key",
                {
                    "pyproject.toml": "[tool.pytest.ini_options]\n"
                    'tmp_path_retention_policy = "all"\n'
                },
                "all",
            ),
            (
                "pyproject-native-table-key",
                {
                    "pyproject.toml": '[tool.pytest]\ntmp_path_retention_policy = "all"\n'
                },
                "all",
            ),
            (
                "a-key-the-file-does-not-set",
                {"pytest.ini": "[pytest]\ntestpaths = tests\n"},
                None,
            ),
            (
                "reads-only-the-file-pytest-picks",
                {
                    "pytest.ini": "[pytest]\ntestpaths = tests\n",
                    "pyproject.toml": "[tool.pytest.ini_options]\n"
                    'tmp_path_retention_policy = "all"\n',
                },
                None,
            ),
        ],
    )
    def test_the_value_read(
        self, tmp_path: Path, case: str, files: dict[str, str], expected: str | None
    ) -> None:
        for name, text in files.items():
            _write(tmp_path / name, text)
        assert ini_value(tmp_path, "tmp_path_retention_policy") == expected

    def test_a_non_string_toml_value_is_stringified(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "pyproject.toml",
            "[tool.pytest.ini_options]\ntmp_path_retention_count = 5\n",
        )
        assert ini_value(tmp_path, "tmp_path_retention_count") == "5"


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

    def test_every_long_spelling_counts(self) -> None:
        tokens = ["--junitxml=a.xml", "--junit-xml", "b.xml"]
        assert option_values(tokens, "--junitxml", "--junit-xml") == [
            "a.xml",
            "b.xml",
        ]

    def test_a_short_only_option(self) -> None:
        tokens = ["-p", "no:xdist", "-pno:junitxml"]
        assert option_values(tokens, "-p") == ["no:xdist", "no:junitxml"]
