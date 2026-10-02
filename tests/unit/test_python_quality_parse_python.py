# Project:   HyperI CI
# File:      tests/unit/test_python_quality_parse_python.py
# Purpose:   Tests for the interpreter bandit and vulture parse source with
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""bandit and vulture parse with the ``ast`` of the Python they run on.

Under uvx's default interpreter, a 3.14 project's PEP 758 ``except A, B:`` was
a syntax error: bandit skipped the file and still exited 0, and vulture
reported the parse failure as a finding.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hyperi_ci.languages.python import quality
from hyperi_ci.languages.quality_common import resolve_tool_cmd

_RUNNING = f"{sys.version_info.major}.{sys.version_info.minor}"

# bandit 1.9.4 under Python 3.12 on a file using PEP 758, trimmed.
_BANDIT_SKIPPED_OUTPUT = """\
Test results:
\tNo issues identified.

Code scanned:
\tTotal lines of code: 7
Files skipped (1):
\tsrc/app/sample.py (syntax error while parsing AST from file)
"""


class TestUvxTakesTheInterpreter:
    def test_python_goes_to_uvx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(
            ["vulture", "src/"], use_uvx=True, spec="vulture==2.16", python="3.14"
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

    def test_no_python_leaves_uvx_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(
            ["vulture", "src/"], use_uvx=True, spec="vulture==2.16"
        )
        assert resolved[:2] == ["uvx", "--from"]

    def test_python_is_ignored_by_the_project_environment_form(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`uv run --with` runs in the project's own venv, which already has one."""
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(
            ["ty", "check"], use_uv_with=True, spec="ty==0.1.0", python="3.14"
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


class TestBanditSkippedFilesAreNamed:
    def test_a_skipped_file_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        said: list[str] = []
        shown: list[str] = []
        monkeypatch.setattr(quality, "warn", said.append)
        monkeypatch.setattr(quality, "info", shown.append)
        quality._warn_bandit_skips(_BANDIT_SKIPPED_OUTPUT)
        assert said == ["  bandit: 1 file(s) could not be parsed and were NOT scanned"]
        assert any("src/app/sample.py" in line for line in shown)

    def test_nothing_skipped_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(quality, "warn", said.append)
        quality._warn_bandit_skips("Test results:\n\tNo issues identified.\n")
        quality._warn_bandit_skips("Files skipped (0):\n")
        quality._warn_bandit_skips(None)
        assert said == []


class _Recorder:
    """What `_run_tool` reported, and the canned result its one command gets."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, result: subprocess.CompletedProcess[str]
    ) -> None:
        self.warned: list[str] = []
        self.errored: list[str] = []
        self.passed: list[str] = []
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(quality, "run_cmd", lambda *_a, **_k: result)
        monkeypatch.setattr(quality, "warn", self.warned.append)
        monkeypatch.setattr(quality, "error", self.errored.append)
        monkeypatch.setattr(quality, "success", self.passed.append)
        monkeypatch.setattr(quality, "info", lambda _msg: None)


def _bandit(mode: str) -> bool:
    return quality._run_tool(
        "bandit", ["bandit", "-r", "src/"], mode, use_uvx=True, spec="bandit==1.9.4"
    )


class TestRunToolActsOnBanditSkips:
    def test_warn_mode_passes_but_names_the_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _Recorder(
            monkeypatch,
            subprocess.CompletedProcess([], 0, _BANDIT_SKIPPED_OUTPUT, ""),
        )
        assert _bandit("warn") is True
        assert rec.warned == [
            "  bandit: 1 file(s) could not be parsed and were NOT scanned"
        ]

    def test_blocking_mode_fails_on_a_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blocking security gate that left a file unread has no clean result."""
        rec = _Recorder(
            monkeypatch,
            subprocess.CompletedProcess([], 0, _BANDIT_SKIPPED_OUTPUT, ""),
        )
        assert _bandit("blocking") is False
        assert rec.errored == ["  bandit: failed, 1 file(s) were not scanned"]
        assert rec.passed == []


class TestAMissingInterpreterIsNotAFinding:
    def test_uv_with_no_interpreter_reads_as_not_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # uv 0.12 with `--python 3.99` and downloads off, stderr verbatim.
        stderr = (
            "error: No interpreter found for Python 3.99 in managed "
            "installations or search path\n"
        )
        rec = _Recorder(monkeypatch, subprocess.CompletedProcess([], 2, "", stderr))
        assert _bandit("warn") is True
        assert rec.warned == ["  bandit: could not start, so it checked nothing"]
