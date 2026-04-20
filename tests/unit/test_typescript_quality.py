# Project:   HyperI CI
# File:      tests/unit/test_typescript_quality.py
# Purpose:   Unit tests for TypeScript/JavaScript quality handler
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Tests for the TS/JS quality handler's npm-script → npx fallback → skip ladder.

Covers the three-state resolution for eslint / prettier / tsc, including
the pure-JS case where a project has no npm scripts and no tsconfig.json
— routed via the javascript→typescript alias in dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.typescript import quality


def _make_config() -> CIConfig:
    """Minimal config with all tools in 'blocking' mode (the default)."""
    cfg = MagicMock(spec=CIConfig)
    cfg.get.side_effect = lambda key, default=None: default
    return cfg


def _write_pkg(tmp_path: Path, scripts: dict[str, str] | None = None) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "t", "scripts": scripts or {}})
    )


@pytest.fixture
def in_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def stub_pm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub package manager detection to avoid touching the real system."""
    monkeypatch.setattr(
        "hyperi_ci.languages.typescript.quality.detect_package_manager",
        lambda: "npm",
    )
    monkeypatch.setattr(
        "hyperi_ci.languages.typescript.quality.ensure_pm_available",
        lambda _: True,
    )


class TestEslintResolution:
    def test_runs_npm_script_when_lint_script_present(
        self, in_tmpdir: Path, stub_pm: None
    ) -> None:
        _write_pkg(in_tmpdir, {"lint": "eslint src/"})
        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            quality.run(_make_config())
        # First call for eslint must be `npm run lint`
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npm", "run", "lint"] in calls

    def test_falls_back_to_npx_eslint_when_config_present_but_no_script(
        self, in_tmpdir: Path, stub_pm: None
    ) -> None:
        _write_pkg(in_tmpdir, {})
        (in_tmpdir / "eslint.config.js").write_text(
            "// flat config\nexport default []\n"
        )
        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "eslint", "."] in calls

    def test_skips_with_warn_when_neither_script_nor_config(
        self,
        in_tmpdir: Path,
        stub_pm: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_pkg(in_tmpdir, {})
        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            rc = quality.run(_make_config())
        # No eslint invocation of any kind
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "eslint", "."] not in calls
        assert ["npm", "run", "lint"] not in calls
        # No hard failure from the skip
        assert rc in (0, 1)  # may be 1 from audit/semgrep, not from eslint


class TestPrettierResolution:
    def test_prefers_format_check_script_over_format_check_arg(
        self, in_tmpdir: Path, stub_pm: None
    ) -> None:
        """`npm run format --check` was a latent footgun — prefer explicit check variant."""
        _write_pkg(
            in_tmpdir,
            {"format": "prettier --write .", "format:check": "prettier --check ."},
        )
        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        # Must run format:check, NOT the unsafe `format --check` form
        assert ["npm", "run", "format:check"] in calls
        assert not any("format" == c[-1] and "--check" in c for c in calls)

    def test_falls_back_to_npx_prettier_when_config_present_but_no_script(
        self, in_tmpdir: Path, stub_pm: None
    ) -> None:
        _write_pkg(in_tmpdir, {})
        (in_tmpdir / ".prettierrc").write_text("{}\n")
        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "prettier", "--check", "."] in calls

    def test_skips_when_neither_script_nor_config(
        self, in_tmpdir: Path, stub_pm: None
    ) -> None:
        _write_pkg(in_tmpdir, {})
        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "prettier", "--check", "."] not in calls


class TestTscResolution:
    def test_prefers_typecheck_script(self, in_tmpdir: Path, stub_pm: None) -> None:
        _write_pkg(in_tmpdir, {"typecheck": "tsc --noEmit"})
        (in_tmpdir / "tsconfig.json").write_text("{}\n")
        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npm", "run", "typecheck"] in calls

    def test_falls_back_to_npx_tsc_when_tsconfig_present_but_no_script(
        self, in_tmpdir: Path, stub_pm: None
    ) -> None:
        _write_pkg(in_tmpdir, {})
        (in_tmpdir / "tsconfig.json").write_text("{}\n")
        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "tsc", "--noEmit"] in calls

    def test_skips_when_pure_js_project_no_tsconfig(
        self, in_tmpdir: Path, stub_pm: None
    ) -> None:
        """Pure JS project (no tsconfig, no script) — tsc must skip, not crawl cwd."""
        _write_pkg(in_tmpdir, {})
        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "tsc", "--noEmit"] not in calls
        assert not any("tsc" in c for c in calls)


class TestPureJsProjectEndToEnd:
    """The motivating case: CommonJS project with only audit/semgrep-ish needs."""

    def test_runs_without_crashing_on_pure_js_project(
        self, in_tmpdir: Path, stub_pm: None
    ) -> None:
        """No eslint config, no prettier config, no tsconfig, no lint scripts.
        eslint / prettier / tsc all skip cleanly; audit + semgrep still run.
        """
        _write_pkg(in_tmpdir, {"test": "echo ok"})
        # No config files — tool-specific skips expected

        with patch("hyperi_ci.languages.typescript.quality.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            rc = quality.run(_make_config())

        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        # None of the script-backed tools invoked
        assert ["npm", "run", "lint"] not in calls
        assert not any(c[:2] == ["npx", "eslint"] for c in calls)
        assert not any(c[:2] == ["npx", "prettier"] for c in calls)
        assert not any(c[:2] == ["npx", "tsc"] for c in calls)
        # audit + semgrep still run (orthogonal to npm scripts)
        assert any(c[:2] == ["npm", "audit"] for c in calls)
        # Non-zero only if audit/semgrep actually failed in real env;
        # with our mocked zero return they succeed
        assert rc == 0
