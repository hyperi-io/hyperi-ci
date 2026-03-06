# Project:   HyperI CI
# File:      tests/unit/test_cli.py
# Purpose:   Tests for CLI argument parsing
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import subprocess
import sys


class TestCLI:
    """CLI entry point tests."""

    def test_version_flag(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "hyperi_ci.cli", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "hyperi-ci" in result.stdout

    def test_detect_in_empty_dir(self, tmp_path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyperi_ci.cli",
                "detect",
                "--project-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_detect_python_project(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyperi_ci.cli",
                "detect",
                "--project-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "python" in result.stdout

    def test_invalid_stage_fails(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "hyperi_ci.cli", "run", "invalid"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0

    def test_config_shows_json(self, tmp_path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("language: rust\n")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyperi_ci.cli",
                "config",
                "--project-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "rust" in result.stdout
