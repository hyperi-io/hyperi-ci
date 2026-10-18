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

import copy
import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig, packaged_default
from hyperi_ci.languages.python import quality
from hyperi_ci.languages.python.quality import (
    _build_pip_audit_cmd,
    _build_ruff_docstring_cmd,
    _build_ruff_format_cmd,
    _build_ruff_security_cmd,
    _repo_ruff_ignores,
)
from hyperi_ci.languages.quality_common import run_gate_tool
from hyperi_ci.quality.ignores import IgnoreEntry
from hyperi_ci.versions import tool_version


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


class TestTyCommand:
    """ty runs inside the project's environment, at the version the SSoT pins."""

    def test_ty_off_path_runs_through_uv_at_the_pinned_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = tmp_path / "calls.jsonl"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # Stands in for uv and uvx: records its argv and exits 0.
        recorder = (
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            f"with open({str(calls)!r}, 'a', encoding='utf-8') as fh:\n"
            "    argv = [pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]\n"
            "    fh.write(json.dumps(argv) + '\\n')\n"
        )
        for name in ("uv", "uvx"):
            (bin_dir / name).write_text(recorder, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.chdir(project)
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.delenv("HYPERCI_QUALITY_SKIP", raising=False)
        monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)

        quality.run(CIConfig(_raw={}))

        argvs = [json.loads(line) for line in calls.read_text().splitlines()]
        assert [argv for argv in argvs if "ty" in argv] == [
            ["uv", "run", "--with", f"ty=={tool_version('ty')}", "--", "ty", "check"]
        ]


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


class TestRuffSecurityCommand:
    """The S pass selects only the bandit rules, over production code."""

    def test_selects_s_over_src(self) -> None:
        assert _build_ruff_security_cmd(["src/"], [], []) == [
            "ruff",
            "check",
            "--select",
            "S",
            "--output-format=concise",
            "src/",
        ]

    def test_handler_excludes_extend_the_repos_own(self) -> None:
        assert _build_ruff_security_cmd(["src/"], ["vendor", "data"], []) == [
            "ruff",
            "check",
            "--select",
            "S",
            "--output-format=concise",
            "src/",
            "--extend-exclude=vendor,data",
        ]

    def test_ruff_ignores_reach_the_pass(self) -> None:
        cmd = _build_ruff_security_cmd(
            ["src/"],
            ["vendor"],
            [
                IgnoreEntry("ruff", "S603", "argv is a list, never a shell"),
                IgnoreEntry("ruff", "S607", "tools resolve off PATH by design"),
            ],
        )
        assert cmd[-2:] == ["--extend-exclude=vendor", "--extend-ignore=S603,S607"]


def _passes(
    monkeypatch: pytest.MonkeyPatch,
    python: dict[str, object],
    *,
    strict: bool = False,
    repo_ignores: list[str] | None = None,
) -> dict[str, tuple[list[str], str, str]]:
    """Run the Python quality stage over the shipped defaults plus ``python``.

    ``repo_ignores`` stands in for the repo's own ruff ignore list, so the
    argv does not depend on the checkout the tests run in.

    Returns each pass's argv, mode and ``via``, by pass name.
    """
    monkeypatch.setattr(quality, "_repo_ruff_ignores", lambda _root: repo_ignores or [])
    monkeypatch.delenv("HYPERCI_QUALITY_SKIP", raising=False)
    if strict:
        monkeypatch.setenv("HYPERCI_QUALITY_STRICT", "1")
    else:
        monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
    monkeypatch.setattr(quality, "_ruff_format_takes_extend_exclude", lambda: True)
    seen: dict[str, tuple[list[str], str, str]] = {}
    default_via = inspect.signature(run_gate_tool).parameters["via"].default

    def record(name: str, cmd: list[str], mode: str, **kw: object) -> bool:
        seen[name] = (cmd, mode, str(kw.get("via", default_via)))
        return True

    monkeypatch.setattr(quality, "run_gate_tool", record)
    raw = copy.deepcopy(packaged_default("quality"))
    raw["python"].update(python)
    raw["exclude_paths"] = ["vendor"]
    raw["ignore"] = [{"tool": "ruff", "id": "S603", "reason": "list argv only"}]
    quality.run(CIConfig(_raw={"quality": raw}))
    return seen


class TestRuffSecurityMode:
    """quality.python.ruff_security decides the pass, independent of `ruff`."""

    def test_ships_warn_with_excludes_and_ignores(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd, mode, _via = _passes(monkeypatch, {})["ruff security"]
        assert mode == "warn"
        assert cmd[:4] == ["ruff", "check", "--select", "S"]
        assert "src/" in cmd
        assert "--extend-ignore=S603" in cmd
        assert any(a.startswith("--extend-exclude=") and "vendor" in a for a in cmd)

    def test_blocking_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        passes = _passes(monkeypatch, {"ruff_security": "blocking"})
        assert passes["ruff security"][1] == "blocking"

    def test_disabled_with_a_reason_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        off = {"mode": "disabled", "reason": "semgrep's Python rules cover it"}
        passes = _passes(monkeypatch, {"ruff_security": off})
        assert passes["ruff security"][1] == "disabled"

    def test_the_lint_key_does_not_move_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        passes = _passes(monkeypatch, {"ruff": "warn"})
        assert passes["ruff check (src)"][1] == "warn"
        assert passes["ruff security"][1] == "warn"
        passes = _passes(monkeypatch, {"ruff_security": "blocking"})
        assert passes["ruff check (src)"][1] == "blocking"

    def test_strict_upgrades_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        passes = _passes(monkeypatch, {}, strict=True)
        assert passes["ruff security"][1] == "blocking"


class TestRuffDocstringsIgnores:
    """`quality.ignore` entries for ruff reach the D pass alongside the repo's own."""

    def test_quality_ignore_entries_reach_the_d_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd, _mode, _via = _passes(monkeypatch, {})["ruff docstrings"]
        assert cmd[:4] == ["ruff", "check", "--select", "D"]
        assert "--extend-ignore=S603" in cmd


class TestRepoIgnoresReachSelectPasses:
    """`--select` drops the config's ignore list, so the S and D passes pass it back."""

    @pytest.mark.parametrize(
        ("build", "rule"),
        [(_build_ruff_security_cmd, "S"), (_build_ruff_docstring_cmd, "D")],
    )
    def test_repo_and_quality_ignores_merge_into_one_flag(self, build, rule) -> None:
        cmd = build(
            ["src/"],
            ["vendor"],
            [IgnoreEntry("ruff", f"{rule}603", "argv is a list")],
            [f"{rule}105", f"{rule}603"],
        )
        assert cmd == [
            "ruff", "check", "--select", rule, "--output-format=concise", "src/",
            "--extend-exclude=vendor", f"--extend-ignore={rule}105,{rule}603",
        ]  # fmt: skip

    @pytest.mark.parametrize(
        "build", [_build_ruff_security_cmd, _build_ruff_docstring_cmd]
    )
    def test_no_ignores_adds_no_flag(self, build) -> None:
        assert not [a for a in build(["src/"], [], [], []) if "ignore" in a]


class TestRepoRuffIgnoresAreRead:
    def test_pyproject_lint_table(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.ruff.lint]\nignore = ["D105"]\nextend-ignore = ["D107"]\n', "utf-8"
        )
        assert _repo_ruff_ignores(tmp_path) == ["D105", "D107"]

    def test_ruff_toml(self, tmp_path: Path) -> None:
        (tmp_path / "ruff.toml").write_text('[lint]\nignore = ["S101"]\n', "utf-8")
        assert _repo_ruff_ignores(tmp_path) == ["S101"]

    def test_legacy_top_level_key(self, tmp_path: Path) -> None:
        (tmp_path / "ruff.toml").write_text('ignore = ["D203"]\n', "utf-8")
        assert _repo_ruff_ignores(tmp_path) == ["D203"]

    def test_extend_chain_is_followed(self, tmp_path: Path) -> None:
        (tmp_path / "base.toml").write_text('[lint]\nignore = ["D100"]\n', "utf-8")
        (tmp_path / "ruff.toml").write_text(
            'extend = "base.toml"\n[lint]\nignore = ["D105"]\n', "utf-8"
        )
        assert _repo_ruff_ignores(tmp_path) == ["D100", "D105"]

    def test_pyproject_without_a_ruff_section(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", "utf-8")
        assert _repo_ruff_ignores(tmp_path) == []

    def test_no_config_or_broken_config(self, tmp_path: Path) -> None:
        assert _repo_ruff_ignores(tmp_path) == []
        (tmp_path / "ruff.toml").write_text("[lint\n", "utf-8")
        assert _repo_ruff_ignores(tmp_path) == []

    def test_the_stage_hands_them_to_both_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        passes = _passes(monkeypatch, {}, repo_ignores=["D105"])
        for name in ("ruff security", "ruff docstrings"):
            flag = [a for a in passes[name][0] if a.startswith("--extend-ignore=")]
            assert flag == ["--extend-ignore=D105,S603"]


@pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None, reason="ruff not importable"
)
class TestRealRuffHonoursTheRepoIgnore:
    """Runs ruff itself, since the defect was ruff's own `--select` behaviour."""

    @staticmethod
    def _findings(project: Path, cmd: list[str]) -> str:
        done = subprocess.run(
            [sys.executable, "-m", *cmd, "--no-cache"],
            cwd=project,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return done.stdout

    @pytest.mark.parametrize(
        ("name", "body"),
        [
            ("pyproject.toml", '[tool.ruff.lint]\nignore = ["D105"]\n'),
            ("ruff.toml", '[lint]\nignore = ["D105"]\n'),
        ],
    )
    def test_d105_is_dropped(self, tmp_path: Path, name: str, body: str) -> None:
        (tmp_path / name).write_text(body, "utf-8")
        (tmp_path / "m.py").write_text(
            '"""Doc."""\n\n\nclass A:\n    """Doc."""\n\n    def __repr__(self):\n'
            '        return "a"\n',
            "utf-8",
        )
        bare = self._findings(
            tmp_path, ["ruff", "check", "--select", "D", "--output-format=concise", "."]
        )
        assert "D105" in bare, "ruff stopped dropping the config ignore on --select"
        cmd = _build_ruff_docstring_cmd(["."], [], [], _repo_ruff_ignores(tmp_path))
        assert "D105" not in self._findings(tmp_path, cmd)


class TestEachPassResolvesAsIntended:
    """A pass run off PATH scans the wrong environment, or nothing at all."""

    @pytest.mark.parametrize(
        ("name", "via"),
        [
            ("ruff check (src)", "uv"),
            ("ruff check (tests/)", "uv"),
            ("ruff format", "uv"),
            ("ty", "uv-with"),
            ("ruff security", "uv"),
            ("pip-audit", "uv"),
            ("ruff docstrings", "uv"),
            ("vulture", "uvx"),
        ],
    )
    def test_via(self, monkeypatch: pytest.MonkeyPatch, name: str, via: str) -> None:
        passes = _passes(monkeypatch, {})
        assert name in passes, sorted(passes)
        assert passes[name][2] == via


class TestRuffFormatBelowTheFlagVersion:
    """ruff under 0.15.21 rejects --extend-exclude, and never walks Markdown."""

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

    def test_the_last_version_that_rejected_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The flag landed in 0.15.21, Markdown formatting in 0.16 -- reading
        # the two as one release drops a project's excludes on 0.15.21-0.15.22.
        assert self._probe(monkeypatch, "ruff 0.15.20\n") is False

    def test_the_first_version_that_takes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._probe(monkeypatch, "ruff 0.15.21\n") is True

    def test_the_version_that_also_walks_markdown(
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
