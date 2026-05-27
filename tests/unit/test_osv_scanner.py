# Project:   HyperI CI
# File:      tests/unit/test_osv_scanner.py
# Purpose:   Tests for the osv-scanner malicious-package helper
#
# License:   Proprietary -- HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for ``hyperi_ci.quality.osv_scanner``."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hyperi_ci.quality import osv_scanner
from hyperi_ci.quality.ignores import IgnoreEntry


class TestRenderIgnoreConfig:
    """Render osv-scanner.toml ``[[IgnoredVulns]]`` blocks from entries."""

    def test_empty_entries_render_empty_string(self) -> None:
        assert osv_scanner.render_ignore_config([]) == ""

    def test_entry_without_expiry_omits_ignore_until(self) -> None:
        out = osv_scanner.render_ignore_config(
            [IgnoreEntry("osv-scanner", "MAL-2026-1", "because")]
        )
        assert "[[IgnoredVulns]]" in out
        assert 'id = "MAL-2026-1"' in out
        assert 'reason = "because"' in out
        assert "ignoreUntil" not in out

    def test_entry_with_expiry_emits_rfc3339_ignore_until(self) -> None:
        out = osv_scanner.render_ignore_config(
            [IgnoreEntry("osv-scanner", "MAL-2026-1", "r", date(2026, 6, 15))]
        )
        assert "ignoreUntil = 2026-06-15T00:00:00Z" in out

    def test_reason_double_quotes_are_escaped(self) -> None:
        out = osv_scanner.render_ignore_config(
            [IgnoreEntry("osv-scanner", "MAL-1", 'he said "hi"')]
        )
        assert r"\"hi\"" in out

    def test_multiple_entries_separated(self) -> None:
        out = osv_scanner.render_ignore_config(
            [
                IgnoreEntry("osv-scanner", "MAL-1", "r1"),
                IgnoreEntry("osv-scanner", "MAL-2", "r2"),
            ]
        )
        assert out.count("[[IgnoredVulns]]") == 2


class TestBuildCommand:
    """Compose the osv-scanner CLI invocation."""

    def test_basic_targets_lockfile(self) -> None:
        cmd = osv_scanner.build_command(Path("Cargo.lock"))
        assert cmd == ["osv-scanner", "scan", "source", "--lockfile", "Cargo.lock"]

    def test_with_config_appends_config_flag(self) -> None:
        cmd = osv_scanner.build_command(Path("Cargo.lock"), Path("/tmp/osv.toml"))
        assert "--config" in cmd
        assert "/tmp/osv.toml" in cmd


class TestRun:
    """Orchestration: detect, (write config), delegate execution + mode."""

    @staticmethod
    def _lockfile(tmp_path: Path, name: str = "Cargo.lock") -> Path:
        lf = tmp_path / name
        lf.write_text("# lockfile\n")
        return lf

    def test_disabled_mode_skips_without_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(osv_scanner, "available", lambda: True)
        called: list[object] = []
        ok = osv_scanner.run(
            self._lockfile(tmp_path),
            [],
            "disabled",
            lambda *a: called.append(a) or False,
        )
        assert ok is True
        assert called == []

    def test_skips_and_passes_when_binary_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(osv_scanner, "available", lambda: False)
        called: list[object] = []
        ok = osv_scanner.run(
            self._lockfile(tmp_path),
            [],
            "blocking",
            lambda *a: called.append(a) or False,
        )
        assert ok is True
        assert called == []

    def test_missing_lockfile_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(osv_scanner, "available", lambda: True)
        called: list[object] = []
        ok = osv_scanner.run(
            tmp_path / "absent.lock",
            [],
            "warn",
            lambda *a: called.append(a) or False,
        )
        assert ok is True
        assert called == []

    def test_no_entries_means_no_config_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(osv_scanner, "available", lambda: True)
        captured: dict[str, object] = {}

        def fake_run_tool(label: str, cmd: list[str], mode: str) -> bool:
            captured["cmd"] = cmd
            captured["mode"] = mode
            return True

        ok = osv_scanner.run(
            self._lockfile(tmp_path), [], "warn", fake_run_tool, write_dir=tmp_path
        )
        assert ok is True
        assert "--config" not in captured["cmd"]  # type: ignore[operator]
        assert captured["mode"] == "warn"

    def test_entries_write_config_and_pass_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(osv_scanner, "available", lambda: True)
        captured: dict[str, object] = {}

        def fake_run_tool(label: str, cmd: list[str], mode: str) -> bool:
            captured["cmd"] = cmd
            return True

        entries = [IgnoreEntry("osv-scanner", "MAL-2026-1", "FP #1276")]
        osv_scanner.run(
            self._lockfile(tmp_path, "pnpm-lock.yaml"),
            entries,
            "warn",
            fake_run_tool,
            write_dir=tmp_path,
        )
        assert "--config" in captured["cmd"]  # type: ignore[operator]
        config = tmp_path / "osv-scanner.toml"
        assert config.exists()
        assert "MAL-2026-1" in config.read_text()
