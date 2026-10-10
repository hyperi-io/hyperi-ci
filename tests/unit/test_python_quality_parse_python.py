# Project:   HyperI CI
# File:      tests/unit/test_python_quality_parse_python.py
# Purpose:   Tests for the interpreter vulture parses source with
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""vulture parses with the ``ast`` of the Python it runs on.

Under uvx's default interpreter, a 3.14 project's PEP 758 ``except A, B:`` was
a syntax error, and vulture reported the parse failure as a finding.
"""

import shutil
import sys
from pathlib import Path

import pytest

from hyperi_ci.languages.python import quality
from hyperi_ci.languages.quality_common import resolve_tool_cmd
from hyperi_ci.versions import runtime_version

_RUNNING = f"{sys.version_info.major}.{sys.version_info.minor}"


class TestUvxTakesTheInterpreter:
    def test_python_goes_to_uvx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(
            ["vulture", "src/"], via="uvx", spec="vulture==2.16", python="3.14"
        )
        assert resolved == [
            "uvx",
            "--python",
            "3.14",
            "--from",
            "vulture==2.16",
            "vulture",
            "src/",
        ]

    def test_no_python_takes_the_baseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Left to itself uvx may pick an interpreter a dependency has no wheel for.
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(["checkov"], via="uvx", spec="checkov==3.3.20")
        assert resolved[:3] == ["uvx", "--python", runtime_version("python")]

    def test_the_unpinned_uvx_fallback_takes_the_baseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
        )
        resolved = resolve_tool_cmd(["ansible-lint"], via="uvx")
        assert resolved[:3] == ["uvx", "--python", runtime_version("python")]

    def test_python_is_ignored_by_the_project_environment_form(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`uv run --with` runs in the project's own venv, which already has one."""
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(
            ["ty", "check"], via="uv-with", spec="ty==0.1.0", python="3.14"
        )
        assert "--python" not in resolved


class TestParsePython:
    def _project(self, root: Path, requires: str) -> None:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "x"\nrequires-python = "{requires}"\n',
            encoding="utf-8",
        )

    def test_a_newer_declared_floor_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._project(tmp_path, ">=3.99")
        monkeypatch.chdir(tmp_path)
        assert quality._parse_python() == "3.99"

    def test_an_older_floor_uses_the_running_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A newer parser still reads older syntax, and needs no download."""
        self._project(tmp_path, ">=3.9")
        monkeypatch.chdir(tmp_path)
        assert quality._parse_python() == _RUNNING

    def test_no_declaration_uses_the_running_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert quality._parse_python() == _RUNNING

    def test_a_pegged_version_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".python-version").write_text("3.99\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert quality._parse_python() == "3.99"

    def test_minor_versions_compare_as_numbers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3.100 is newer than 3.14, which a string comparison gets wrong."""
        self._project(tmp_path, ">=3.100")
        monkeypatch.chdir(tmp_path)
        assert quality._parse_python() == "3.100"
