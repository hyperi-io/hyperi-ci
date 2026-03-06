# Project:   HyperI CI
# File:      tests/unit/test_detect.py
# Purpose:   Tests for language detection
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.detect import detect_language


class TestDetectLanguage:
    """Language detection from file markers."""

    def test_detects_python_from_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        assert detect_language(tmp_path) == "python"

    def test_detects_python_from_setup_py(self, tmp_path: Path) -> None:
        (tmp_path / "setup.py").write_text("from setuptools import setup\n")
        assert detect_language(tmp_path) == "python"

    def test_detects_rust_from_cargo(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'test'\n")
        assert detect_language(tmp_path) == "rust"

    def test_detects_typescript_from_tsconfig(self, tmp_path: Path) -> None:
        (tmp_path / "tsconfig.json").write_text("{}")
        (tmp_path / "package.json").write_text("{}")
        assert detect_language(tmp_path) == "typescript"

    def test_detects_javascript_from_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        assert detect_language(tmp_path) == "javascript"

    def test_typescript_wins_over_javascript(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "tsconfig.json").write_text("{}")
        assert detect_language(tmp_path) == "typescript"

    def test_detects_golang_from_go_mod(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/test\n")
        assert detect_language(tmp_path) == "golang"

    def test_detects_bash_from_bats_tests(self, tmp_path: Path) -> None:
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_basic.bats").write_text("@test 'example' { true; }\n")
        assert detect_language(tmp_path) == "bash"

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
