# Project:   HyperI CI
# File:      tests/unit/test_quality_ignore_wiring.py
# Purpose:   Command-builder tests for the Python quality handler
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Command-builder tests for the Python quality handler.

Verifies the argv each builder hands to its tool: ``quality.ignore``
entries translated to the tool's native flag, and the excludes the
format gate carries.
"""

from __future__ import annotations

import subprocess

import pytest

from hyperi_ci.languages.python import quality
from hyperi_ci.languages.python.quality import (
    _build_pip_audit_cmd,
    _build_ruff_format_cmd,
)
from hyperi_ci.quality.ignores import IgnoreEntry


class TestPipAuditCommand:
    """pip-audit translates each ignore entry to --ignore-vuln <id>."""

    def test_no_ignores_yields_bare_command(self) -> None:
        cmd = _build_pip_audit_cmd([])
        assert "--ignore-vuln" not in cmd
        assert "pip-audit" in cmd

    def test_one_ignore_added(self) -> None:
        cmd = _build_pip_audit_cmd(
            [IgnoreEntry("pip-audit", "PYSEC-2025-183", "Disputed")]
        )
        assert cmd.count("--ignore-vuln") == 1
        idx = cmd.index("--ignore-vuln")
        assert cmd[idx + 1] == "PYSEC-2025-183"

    def test_multiple_ignores_each_get_their_own_flag(self) -> None:
        cmd = _build_pip_audit_cmd(
            [
                IgnoreEntry("pip-audit", "PYSEC-A", "r1"),
                IgnoreEntry("pip-audit", "PYSEC-B", "r2"),
            ]
        )
        assert cmd.count("--ignore-vuln") == 2
        # Both ids appear after their respective flags
        assert "PYSEC-A" in cmd
        assert "PYSEC-B" in cmd


class TestRuffFormatCommand:
    """ruff format adds Markdown to the repo's excludes rather than replacing them."""

    def test_markdown_only(self) -> None:
        assert _build_ruff_format_cmd([]) == [
            "ruff",
            "format",
            "--check",
            ".",
            "--extend-exclude=*.md",
        ]

    def test_handler_excludes_are_preserved(self) -> None:
        assert _build_ruff_format_cmd(["vendor"]) == [
            "ruff",
            "format",
            "--check",
            ".",
            "--extend-exclude=vendor,*.md",
        ]


class TestRuffFormatBelowTheFlagVersion:
    """ruff under 0.16 rejects --extend-exclude, and never walks Markdown."""

    def test_the_flag_is_dropped_entirely(self) -> None:
        assert _build_ruff_format_cmd([], extend_exclude=False) == [
            "ruff",
            "format",
            "--check",
            ".",
        ]

    def test_project_excludes_go_with_it(self) -> None:
        assert _build_ruff_format_cmd(["vendor"], extend_exclude=False) == [
            "ruff",
            "format",
            "--check",
            ".",
        ]


class TestRuffVersionProbe:
    """The probe reads the RESOLVED ruff -- hyperi-ci pins none for consumers."""

    @staticmethod
    def _probe(
        monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int = 0
    ) -> bool:
        def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess([], returncode, stdout, "")

        monkeypatch.setattr(quality.subprocess, "run", fake_run)
        return quality._ruff_format_takes_extend_exclude()

    def test_the_version_that_rejected_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._probe(monkeypatch, "ruff 0.15.12\n") is False

    def test_the_first_version_that_takes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._probe(monkeypatch, "ruff 0.16.0\n") is True

    def test_a_newer_ruff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._probe(monkeypatch, "ruff 0.17.3\n") is True

    def test_an_unreadable_version_keeps_the_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._probe(monkeypatch, "ruff banana\n") is True

    def test_no_output_keeps_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._probe(monkeypatch, "") is True

    def test_a_failed_probe_keeps_the_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._probe(monkeypatch, "ruff 0.15.12\n", returncode=1) is True


class TestArgumentRejectionIsNotAFinding:
    """A refused flag means the tool never ran, so it is not a finding (#146)."""

    _CLAP = "error: unexpected argument '--extend-exclude' found\n"

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch, stderr: str, mode: str
    ) -> tuple[bool, list[str]]:
        messages: list[str] = []

        def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess([], 2, "", stderr)

        monkeypatch.setattr(quality.subprocess, "run", fake_run)
        monkeypatch.setattr(quality.shutil, "which", lambda _cmd: "/usr/bin/ruff")
        # Loguru bypasses capsys -- capture via the module's own log names.
        monkeypatch.setattr(quality, "error", messages.append)
        monkeypatch.setattr(quality, "warn", messages.append)
        ok = quality._run_tool("ruff format", ["ruff", "format"], mode)
        return ok, messages

    def test_blocking_names_the_mismatch_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ok, messages = self._run(monkeypatch, self._CLAP, "blocking")
        assert ok is False
        assert any("tool-version mismatch" in m for m in messages)
        assert not any(m.endswith("failed") for m in messages)

    def test_warn_mode_still_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ok, messages = self._run(monkeypatch, self._CLAP, "warn")
        assert ok is True
        assert any("tool-version mismatch" in m for m in messages)

    def test_a_real_finding_still_reads_as_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ok, messages = self._run(monkeypatch, "would reformat: x.py\n", "blocking")
        assert ok is False
        assert any(m.endswith("failed") for m in messages)
