# Project:   HyperI CI
# File:      tests/unit/test_typescript_quality.py
# Purpose:   Unit tests for TypeScript/JavaScript quality handler
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Tests for the TS/JS quality handler's npm-script → npx fallback → skip ladder.

Covers the three-state resolution for eslint / prettier / tsc, including
the pure-JS case where a project has no npm scripts and no tsconfig.json
-- routed via the javascript→typescript alias in dispatch.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hyperi_ci import tools
from hyperi_ci.config import CIConfig, shipped_default
from hyperi_ci.languages import quality_common
from hyperi_ci.languages.typescript import quality


def _make_config() -> CIConfig:
    """Minimal config with all tools in 'blocking' mode (the default)."""
    cfg = MagicMock(spec=CIConfig)
    cfg.get.side_effect = lambda key, default=None: default
    cfg.setting.side_effect = shipped_default
    cfg._raw = {}
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


@pytest.fixture
def mock_run(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Every gate tool is installed and passes; the mock records each command."""
    installed = {"npm", "npx", "pnpm", "yarn"}
    monkeypatch.setattr(
        quality_common.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in installed else None,
    )
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(quality_common, "run_cmd", run)
    return run


class TestEslintResolution:
    def test_runs_npm_script_when_lint_script_present(
        self, in_tmpdir: Path, stub_pm: None, mock_run: MagicMock
    ) -> None:
        _write_pkg(in_tmpdir, {"lint": "eslint src/"})
        quality.run(_make_config())
        # First call for eslint must be `npm run lint`
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npm", "run", "lint"] in calls

    def test_falls_back_to_npx_eslint_when_config_present_but_no_script(
        self, in_tmpdir: Path, stub_pm: None, mock_run: MagicMock
    ) -> None:
        _write_pkg(in_tmpdir, {})
        (in_tmpdir / "eslint.config.js").write_text(
            "// flat config\nexport default []\n"
        )
        quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "eslint", "."] in calls

    def test_skips_with_warn_when_neither_script_nor_config(
        self,
        in_tmpdir: Path,
        stub_pm: None,
        mock_run: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_pkg(in_tmpdir, {})
        rc = quality.run(_make_config())
        # No eslint invocation of any kind
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "eslint", "."] not in calls
        assert ["npm", "run", "lint"] not in calls
        # No hard failure from the skip
        assert rc in (0, 1)  # may be 1 from audit, not from eslint


class TestPrettierResolution:
    def test_prefers_format_check_script_over_format_check_arg(
        self, in_tmpdir: Path, stub_pm: None, mock_run: MagicMock
    ) -> None:
        """`npm run format --check` was a latent footgun -- prefer explicit check variant."""
        _write_pkg(
            in_tmpdir,
            {"format": "prettier --write .", "format:check": "prettier --check ."},
        )
        quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        # Must run format:check, NOT the unsafe `format --check` form
        assert ["npm", "run", "format:check"] in calls
        assert not any("format" == c[-1] and "--check" in c for c in calls)

    def test_falls_back_to_npx_prettier_when_config_present_but_no_script(
        self, in_tmpdir: Path, stub_pm: None, mock_run: MagicMock
    ) -> None:
        _write_pkg(in_tmpdir, {})
        (in_tmpdir / ".prettierrc").write_text("{}\n")
        quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "prettier", "--check", "."] in calls

    def test_skips_when_neither_script_nor_config(
        self, in_tmpdir: Path, stub_pm: None, mock_run: MagicMock
    ) -> None:
        _write_pkg(in_tmpdir, {})
        quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "prettier", "--check", "."] not in calls


class TestTscResolution:
    def test_prefers_typecheck_script(
        self, in_tmpdir: Path, stub_pm: None, mock_run: MagicMock
    ) -> None:
        _write_pkg(in_tmpdir, {"typecheck": "tsc --noEmit"})
        (in_tmpdir / "tsconfig.json").write_text("{}\n")
        quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npm", "run", "typecheck"] in calls

    def test_falls_back_to_npx_tsc_when_tsconfig_present_but_no_script(
        self, in_tmpdir: Path, stub_pm: None, mock_run: MagicMock
    ) -> None:
        _write_pkg(in_tmpdir, {})
        (in_tmpdir / "tsconfig.json").write_text("{}\n")
        quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "tsc", "--noEmit"] in calls

    def test_skips_when_pure_js_project_no_tsconfig(
        self, in_tmpdir: Path, stub_pm: None, mock_run: MagicMock
    ) -> None:
        """Pure JS project (no tsconfig, no script) -- tsc must skip, not crawl cwd."""
        _write_pkg(in_tmpdir, {})
        quality.run(_make_config())
        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        assert ["npx", "tsc", "--noEmit"] not in calls
        assert not any("tsc" in c for c in calls)


class TestPureJsProjectEndToEnd:
    """The motivating case: CommonJS project with only audit-ish needs."""

    def test_runs_without_crashing_on_pure_js_project(
        self, in_tmpdir: Path, stub_pm: None, mock_run: MagicMock
    ) -> None:
        """No eslint config, no prettier config, no tsconfig, no lint scripts.
        eslint / prettier / tsc all skip cleanly; audit still runs (semgrep is
        now a cross-language dispatch-level scan, not a TS-handler step).
        """
        _write_pkg(in_tmpdir, {"test": "echo ok"})
        # No config files -- tool-specific skips expected

        # osv-scanner is the one tool here not stubbed through the gate runner;
        # left real, the result depended on whether this machine had it.
        with patch(
            "hyperi_ci.languages.typescript.quality.osv_scanner.run",
            return_value=True,
        ):
            rc = quality.run(_make_config())

        calls = [list(c.args[0]) for c in mock_run.call_args_list]
        # None of the script-backed tools invoked
        assert ["npm", "run", "lint"] not in calls
        assert not any(c[:2] == ["npx", "eslint"] for c in calls)
        assert not any(c[:2] == ["npx", "prettier"] for c in calls)
        assert not any(c[:2] == ["npx", "tsc"] for c in calls)
        # audit still runs (orthogonal to npm scripts)
        assert any(c[:2] == ["npm", "audit"] for c in calls)
        # Non-zero only if audit actually failed in real env;
        # with our mocked zero return it succeeds
        assert rc == 0


class TestAMissingToolGates:
    """A runner without npx gates like every other language instead of crashing."""

    @pytest.fixture
    def no_npx(
        self,
        in_tmpdir: Path,
        stub_pm: None,
        mock_run: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> MagicMock:
        _write_pkg(in_tmpdir, {})
        (in_tmpdir / "eslint.config.js").write_text("export default []\n")
        monkeypatch.setattr(
            quality_common.shutil,
            "which",
            lambda name: "/usr/bin/npm" if name == "npm" else None,
        )
        monkeypatch.setattr(quality, "osv_scanner", MagicMock())
        return mock_run

    def test_ci_fails_the_blocking_gate(
        self, no_npx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "is_ci", lambda: True)
        assert quality.run(_make_config()) == 1
        calls = [list(c.args[0]) for c in no_npx.call_args_list]
        assert not any(c[0] == "npx" for c in calls)

    def test_local_run_skips_it(
        self, no_npx: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "is_ci", lambda: False)
        assert quality.run(_make_config()) == 0
        calls = [list(c.args[0]) for c in no_npx.call_args_list]
        assert calls == [["npm", "audit", "--audit-level=moderate"]]


class TestAuditCommand:
    """`_audit_command` builds the right invocation per package manager."""

    @pytest.mark.parametrize(
        ("pm", "yarn_major", "expected"),
        [
            ("npm", 0, ["npm", "audit", "--audit-level=moderate"]),
            ("pnpm", 0, ["pnpm", "audit", "--audit-level=moderate"]),
            ("yarn", 1, ["yarn", "audit", "--audit-level=moderate"]),
            ("yarn", 2, ["yarn", "npm", "audit", "--severity", "moderate"]),
            ("yarn", 4, ["yarn", "npm", "audit", "--severity", "moderate"]),
        ],
    )
    def test_command_per_package_manager(
        self, pm: str, yarn_major: int, expected: list[str]
    ) -> None:
        cmd = quality._audit_command(
            audit_level="moderate", pm=pm, yarn_major=yarn_major
        )
        assert cmd == expected

    def test_audit_level_is_threaded_through(self) -> None:
        assert quality._audit_command(audit_level="high", pm="yarn", yarn_major=4) == [
            "yarn",
            "npm",
            "audit",
            "--severity",
            "high",
        ]


def _shipped_config(**typescript: object) -> CIConfig:
    """Config carrying the shipped quality defaults, with typescript keys overridden."""
    shipped = shipped_default("quality")
    shipped["typescript"].update(typescript)
    return CIConfig(_raw={"quality": shipped})


def _write_manifest(
    tmp_path: Path,
    scripts: dict[str, object] | None = None,
    dev_deps: dict[str, str] | None = None,
) -> None:
    manifest = {
        "name": "t",
        "scripts": scripts or {},
        "devDependencies": dev_deps or {},
    }
    (tmp_path / "package.json").write_text(json.dumps(manifest), encoding="utf-8")


class _Gates:
    """Records each gate the handler runs as (label, command, mode)."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, list[str], str]] = []

    def record(self, tool_name: str, cmd: list[str], mode: str, **_kw: object) -> bool:
        self.runs.append((tool_name, cmd, mode))
        return True

    def lint_and_format(self) -> list[tuple[str, list[str], str]]:
        """The runs other than tsc and audit."""
        return [r for r in self.runs if r[0] not in ("tsc", "audit")]


@pytest.fixture
def gates(in_tmpdir: Path, stub_pm: None, monkeypatch: pytest.MonkeyPatch) -> _Gates:
    rec = _Gates()
    monkeypatch.setattr(quality, "run_gate_tool", rec.record)
    monkeypatch.setattr(quality.osv_scanner, "run", lambda *_a, **_kw: True)
    return rec


class TestScriptTools:
    """`_script_tools` names what a script body actually runs."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("vp lint src/", ["oxlint"]),
            ("vp fmt --check src/", ["oxfmt"]),
            ("vp format --check", ["oxfmt"]),
            ("vp check", ["oxlint", "oxfmt"]),
            ("oxlint src/", ["oxlint"]),
            ("oxfmt --check .", ["oxfmt"]),
            ("oxlint && eslint .", ["oxlint", "eslint"]),
            ("eslint . && oxlint", ["eslint", "oxlint"]),
            ("prettier --check . || true", ["prettier"]),
            ("pnpm exec oxlint --deny-warnings", ["oxlint"]),
            ("node_modules/.bin/oxlint src", ["oxlint"]),
            (
                "eslint --config eslint.config.js --plugin eslint-plugin-oxlint .",
                ["eslint"],
            ),
            ("oxlint src && oxlint test", ["oxlint"]),
            ("turbo run lint", []),
            ("vp run lint", []),
            ("vp", []),
            ("", []),
        ],
    )
    def test_detects_the_invoked_tools(self, body: str, expected: list[str]) -> None:
        assert quality._script_tools(body) == expected


class TestScriptLabels:
    """A lint or format script is reported under the tools it runs."""

    def test_vite_plus_scripts_report_oxlint_and_oxfmt(self, gates: _Gates) -> None:
        _write_manifest(
            Path.cwd(),
            {"lint": "vp lint src/", "format:check": "vp fmt --check src/"},
            {"vite-plus": "1.1.0", "oxlint": "1.87.0", "oxfmt": "0.72.0"},
        )
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [
            ("oxlint", ["npm", "run", "lint"], "blocking"),
            ("oxfmt", ["npm", "run", "format:check"], "warn"),
        ]

    def test_plain_oxlint_script(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd(), {"lint": "oxlint src/"})
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [
            ("oxlint", ["npm", "run", "lint"], "blocking")
        ]

    def test_vp_check_reports_both_tools(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd(), {"lint": "vp check"})
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [
            ("oxlint+oxfmt", ["npm", "run", "lint"], "blocking")
        ]

    def test_oxlint_beside_eslint_reports_both(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd(), {"lint": "oxlint && eslint ."})
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [
            ("oxlint+eslint", ["npm", "run", "lint"], "blocking")
        ]

    @pytest.mark.parametrize(
        ("oxlint", "eslint", "expected"),
        [
            ("warn", "blocking", "blocking"),
            ("blocking", "warn", "blocking"),
            ("warn", "disabled", "warn"),
            ("disabled", "disabled", "disabled"),
        ],
    )
    def test_combined_label_takes_the_strictest_mode(
        self, gates: _Gates, oxlint: str, eslint: str, expected: str
    ) -> None:
        _write_manifest(Path.cwd(), {"lint": "oxlint && eslint ."})
        quality.run(_shipped_config(oxlint=oxlint, eslint=eslint))
        assert gates.lint_and_format() == [
            ("oxlint+eslint", ["npm", "run", "lint"], expected)
        ]

    def test_oxfmt_mode_is_its_own_key(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd(), {"format:check": "oxfmt --check ."})
        quality.run(_shipped_config(oxfmt="blocking", prettier="disabled"))
        assert gates.lint_and_format() == [
            ("oxfmt", ["npm", "run", "format:check"], "blocking")
        ]

    @pytest.mark.parametrize("body", ["turbo run lint", 5])
    def test_unrecognised_lint_script_keeps_eslint(
        self, gates: _Gates, body: object
    ) -> None:
        _write_manifest(Path.cwd(), {"lint": body})
        quality.run(_shipped_config(eslint="warn"))
        assert gates.lint_and_format() == [("eslint", ["npm", "run", "lint"], "warn")]

    def test_unrecognised_format_script_keeps_prettier(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd(), {"check:format": "biome format ."})
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [
            ("prettier", ["npm", "run", "check:format"], "blocking")
        ]


class TestOxcFallbacks:
    """No script: oxlint and Oxfmt run through npx from a config or devDependency."""

    @pytest.mark.parametrize("marker", [".oxlintrc.json", "oxlint.config.ts"])
    def test_oxlint_config_runs_npx_oxlint(self, gates: _Gates, marker: str) -> None:
        _write_manifest(Path.cwd())
        Path(marker).write_text("{}\n", encoding="utf-8")
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [("oxlint", ["npx", "oxlint"], "blocking")]

    def test_oxlint_dev_dependency_runs_npx_oxlint(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd(), dev_deps={"oxlint": "1.87.0"})
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [("oxlint", ["npx", "oxlint"], "blocking")]

    @pytest.mark.parametrize("marker", [".oxfmtrc.json", ".oxfmtrc.jsonc"])
    def test_oxfmt_config_runs_npx_oxfmt_at_warn(
        self, gates: _Gates, marker: str
    ) -> None:
        _write_manifest(Path.cwd())
        Path(marker).write_text("{}\n", encoding="utf-8")
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [
            ("oxfmt", ["npx", "oxfmt", "--check"], "warn")
        ]

    def test_oxfmt_dev_dependency_runs_npx_oxfmt(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd(), dev_deps={"oxfmt": "0.72.0"})
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [
            ("oxfmt", ["npx", "oxfmt", "--check"], "warn")
        ]

    def test_vite_plus_runs_vp_lint_and_vp_fmt(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd(), dev_deps={"vite-plus": "1.1.0", "oxlint": "1.87.0"})
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [
            ("oxlint", ["npx", "vp", "lint"], "blocking"),
            ("oxfmt", ["npx", "vp", "fmt", "--check"], "warn"),
        ]

    def test_oxlint_and_eslint_configs_run_both(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd(), dev_deps={"oxlint": "1.87.0"})
        Path("eslint.config.js").write_text("export default []\n", encoding="utf-8")
        quality.run(_shipped_config())
        assert gates.lint_and_format() == [
            ("oxlint", ["npx", "oxlint"], "blocking"),
            ("eslint", ["npx", "eslint", "."], "blocking"),
        ]

    def test_nothing_configured_runs_no_lint_or_format(self, gates: _Gates) -> None:
        _write_manifest(Path.cwd())
        quality.run(_shipped_config())
        assert gates.lint_and_format() == []


def test_shipped_modes() -> None:
    """oxlint gates like eslint; Oxfmt is pre-1.0, so it ships at warn."""
    assert shipped_default("quality.typescript.oxlint") == "blocking"
    assert shipped_default("quality.typescript.oxfmt") == "warn"
