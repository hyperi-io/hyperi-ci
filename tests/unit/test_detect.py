# Project:   HyperI CI
# File:      tests/unit/test_detect.py
# Purpose:   Tests for language detection
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path

import pytest

from hyperi_ci import detect
from hyperi_ci.detect import detect_language


class TestDetectLanguage:
    """Language detection from file markers."""

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            ({"pyproject.toml": "[project]\nname = 'test'\n"}, "python"),
            ({"setup.py": "from setuptools import setup\n"}, "python"),
            ({"Cargo.toml": "[package]\nname = 'test'\n"}, "rust"),
            ({"tsconfig.json": "{}", "package.json": "{}"}, "typescript"),
            ({"package.json": "{}"}, "javascript"),
            ({"go.mod": "module example.com/test\n"}, "golang"),
        ],
        ids=["pyproject", "setup-py", "cargo", "tsconfig", "package-json", "go-mod"],
    )
    def test_detects_the_language_from_its_marker(
        self, tmp_path: Path, files: dict[str, str], expected: str
    ) -> None:
        for name, text in files.items():
            (tmp_path / name).write_text(text)
        assert detect_language(tmp_path) == expected

    def test_typescript_wins_over_javascript(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "tsconfig.json").write_text("{}")
        assert detect_language(tmp_path) == "typescript"

    def test_bats_tests_alone_are_not_a_language(self, tmp_path: Path) -> None:
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_basic.bats").write_text("@test 'example' { true; }\n")
        assert detect_language(tmp_path) is None

    def test_retired_hypersec_names_are_not_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERSEC_CI_LANGUAGE", "rust")
        (tmp_path / ".hypersec-ci.yaml").write_text("language: golang\n")
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        assert detect_language(tmp_path) == "python"

    def test_returns_none_for_empty_dir(self, tmp_path: Path) -> None:
        assert detect_language(tmp_path) is None

    def test_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERI_CI_LANGUAGE", "rust")
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        assert detect_language(tmp_path) == "rust"

    def test_config_file_override(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("language: golang\n")
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        assert detect_language(tmp_path) == "golang"


class TestAMalformedConfigFileIsNotFatal:
    """A config file detection cannot parse warns and falls through, never crashes."""

    def test_malformed_yaml_warns_and_falls_back_to_marker_detection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / ".hyperi-ci.yaml"
        config_file.write_text("language: [unterminated\n")
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        warned: list[str] = []
        monkeypatch.setattr(detect, "warn", warned.append)
        assert detect_language(tmp_path) == "python"
        assert any(str(config_file) in w for w in warned)

    def test_an_unreadable_config_warns_and_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / ".hyperi-ci.yaml"
        config_dir.mkdir()  # exists(), but open() raises IsADirectoryError
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        warned: list[str] = []
        monkeypatch.setattr(detect, "warn", warned.append)
        assert detect_language(tmp_path) == "python"
        assert any(str(config_dir) in w for w in warned)
