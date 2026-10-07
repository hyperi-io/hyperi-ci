# Project:   HyperI CI
# File:      tests/unit/test_project_config.py
# Purpose:   Reading the project config where hyperi-ci is not installed
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The stdlib reader the predict-version helpers load by path.

A config that exists and cannot be read must say why. Reading it as empty is
how the ARC vanilla image, with neither PyYAML nor yq, handed every Plan the
defaults in silence (issue #291).
"""

import sys
from pathlib import Path

import pytest

from hyperi_ci import project_config
from hyperi_ci.project_config import (
    CONFIG_FILES,
    NO_PARSER,
    ProjectConfig,
    read_project_config,
)


@pytest.fixture
def no_pyyaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import yaml`` fail, as on a runner whose python3 lacks it."""
    monkeypatch.setitem(sys.modules, "yaml", None)


def _write(root: Path, text: str, name: str = ".hyperi-ci.yaml") -> Path:
    (root / name).write_text(text, encoding="utf-8")
    return root


class TestReading:
    def test_no_config_at_all_is_empty_and_not_a_problem(self, tmp_path: Path) -> None:
        # Most repos carry no .hyperi-ci.yaml, and that is legitimate.
        assert read_project_config(tmp_path) == ProjectConfig({}, CONFIG_FILES[0], "")

    def test_an_empty_file_is_empty(self, tmp_path: Path) -> None:
        assert read_project_config(_write(tmp_path, "")).data == {}

    def test_a_mapping_is_read(self, tmp_path: Path) -> None:
        project = read_project_config(_write(tmp_path, "test:\n  tier: full\n"))
        assert project.data == {"test": {"tier": "full"}}
        assert project.unreadable == ""

    def test_the_first_spelling_wins(self, tmp_path: Path) -> None:
        _write(tmp_path, "a: 1\n", ".hyperi-ci.yaml")
        _write(tmp_path, "a: 2\n", ".hyperi-ci.yml")
        assert read_project_config(tmp_path) == ProjectConfig(
            {"a": 1}, ".hyperi-ci.yaml", ""
        )

    def test_the_retired_hypersec_spelling_is_not_read(self, tmp_path: Path) -> None:
        _write(tmp_path, "a: 2\n", ".hypersec-ci.yaml")
        assert read_project_config(tmp_path).data == {}


class TestUnreadable:
    def test_invalid_yaml_names_the_file_and_the_error_on_one_line(
        self, tmp_path: Path
    ) -> None:
        project = read_project_config(_write(tmp_path, "test: [unclosed\n"))
        assert project.data is None
        assert project.unreadable.startswith(
            ".hyperi-ci.yaml could not be read: it is not valid YAML:"
        )
        # It ends up in a ::warning:: line, which a newline would cut short.
        assert "\n" not in project.unreadable

    def test_a_list_at_the_top_is_not_a_config(self, tmp_path: Path) -> None:
        project = read_project_config(_write(tmp_path, "- one\n- two\n"))
        assert project.data is None
        assert "list, not a mapping" in project.problem

    def test_no_parser_at_all_says_so(
        self, tmp_path: Path, no_pyyaml: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(project_config.shutil, "which", lambda _: None)
        project = read_project_config(_write(tmp_path, "test:\n  tier: full\n"))
        assert project.data is None
        assert project.problem == NO_PARSER
        assert project.unreadable == f".hyperi-ci.yaml could not be read: {NO_PARSER}"

    def test_no_parser_with_no_config_is_still_fine(
        self, tmp_path: Path, no_pyyaml: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With nothing to parse, a missing parser costs nothing and says nothing.
        monkeypatch.setattr(project_config.shutil, "which", lambda _: None)
        assert read_project_config(tmp_path) == ProjectConfig({}, CONFIG_FILES[0], "")

    def test_yq_answers_when_pyyaml_is_missing(
        self, tmp_path: Path, no_pyyaml: None
    ) -> None:
        if project_config.shutil.which("yq") is None:
            pytest.skip("needs yq")
        project = read_project_config(_write(tmp_path, "test:\n  tier: full\n"))
        assert project.data == {"test": {"tier": "full"}}

    def test_a_yq_that_fails_says_so(
        self, tmp_path: Path, no_pyyaml: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = tmp_path / "bin"
        fake.mkdir()
        yq = fake / "yq"
        yq.write_text(
            "#!/bin/sh\necho 'Error: bad file' >&2\nexit 1\n", encoding="utf-8"
        )
        yq.chmod(0o755)
        monkeypatch.setenv("PATH", str(fake))
        project = read_project_config(_write(tmp_path, "a: 1\n"))
        assert project.data is None
        assert project.problem == "yq could not parse it: Error: bad file"
