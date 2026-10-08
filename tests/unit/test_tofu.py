# Project:   HyperI CI
# File:      tests/unit/test_tofu.py
# Purpose:   Tests for the OpenTofu dimension of lint-iac
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.tofu.

Root detection and the command lines run against real files with run_cmd
recorded; the gate behaviour against real tofu is in test_lint_iac.py.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import findings, tofu


def _tf(path: Path, text: str = 'variable "x" {}\n') -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "main.tf").write_text(text, encoding="utf-8")
    return path


def _not_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE"):
        monkeypatch.delenv(name, raising=False)


class TestRoots:
    def test_a_locally_called_module_is_not_a_root(self, tmp_path: Path) -> None:
        env = _tf(
            tmp_path / "environments" / "aws",
            'module "net" {\n  source = "../../modules/net"\n}\n',
        )
        net = _tf(tmp_path / "modules" / "net")
        orphan = _tf(tmp_path / "modules" / "orphan")
        assert tofu.roots([env, net, orphan]) == [env, orphan]

    @pytest.mark.parametrize("comment", ["# local", "// local"])
    def test_a_trailing_comment_still_counts_as_a_call(
        self, tmp_path: Path, comment: str
    ) -> None:
        env = _tf(tmp_path / "env", f'module "m" {{\n  source = "../m" {comment}\n}}\n')
        m = _tf(tmp_path / "m")
        assert tofu.roots([env, m]) == [env]

    def test_a_registry_source_calls_nothing_local(self, tmp_path: Path) -> None:
        env = _tf(tmp_path / "env", 'module "x" {\n  source = "hashicorp/x/aws"\n}\n')
        assert tofu.roots([env]) == [env]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Record every tofu call; validate reports one error diagnostic."""
    state: dict = {"calls": [], "validate": '{"valid": true, "diagnostics": []}'}

    def _run(cmd: list[str], **kw: Any) -> Any:
        state["calls"].append((cmd, kw))
        if cmd[1] == "validate":
            return SimpleNamespace(returncode=0, stdout=state["validate"], stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(findings, "run_cmd", _run)
    monkeypatch.setattr(tofu, "ci_binary", lambda name: "tofu")
    monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", str(tmp_path / "plugins"))
    return state


class TestCommands:
    def test_init_is_backend_free_and_data_dir_is_scratch(
        self, recorded: dict, tmp_path: Path
    ) -> None:
        root = _tf(tmp_path / "repo")
        tofu.run([root], CIConfig(_raw={}), scratch=tmp_path / "s", timeout=7)
        init_cmd, init_kw = next(c for c in recorded["calls"] if c[0][1] == "init")
        assert "-backend=false" in init_cmd and "-input=false" in init_cmd
        assert "-lockfile=readonly" not in init_cmd
        assert init_kw["env"]["TF_DATA_DIR"].startswith(str(tmp_path / "s"))
        assert init_kw["env"]["TF_PLUGIN_CACHE_DIR"] == str(tmp_path / "plugins")
        assert init_kw["timeout"] == 7
        assert not any(c[0][1] in ("plan", "apply") for c in recorded["calls"])

    def test_init_and_validate_run_on_a_scratch_copy(
        self, recorded: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        root = _tf(repo / "env", 'module "m" {\n  source = "../modules/m"\n}\n')
        _tf(repo / "modules" / "m")
        (root / ".terraform.lock.hcl").write_text("# lock\n", encoding="utf-8")
        seen: dict = {}
        original = findings.run_cmd

        def _look(cmd: list[str], **kw: Any) -> Any:
            if cmd[1] == "init":
                cwd = Path(kw["cwd"])
                seen["cwd"] = cwd
                seen["lock"] = (cwd / ".terraform.lock.hcl").is_file()
                seen["module"] = (cwd.parent / "modules" / "m" / "main.tf").is_file()
            return original(cmd, **kw)

        monkeypatch.setattr(findings, "run_cmd", _look)
        monkeypatch.chdir(repo)
        tofu.run([root], CIConfig(_raw={}), scratch=tmp_path / "s")
        assert seen["cwd"] == tmp_path / "s" / "tofu-0" / "env"
        assert seen["lock"] and seen["module"]
        assert not (tmp_path / "s" / "tofu-0").exists()
        assert sorted(p.name for p in root.iterdir()) == [
            ".terraform.lock.hcl",
            "main.tf",
        ]

    def test_validate_diagnostics_fail_a_blocking_gate(
        self, recorded: dict, tmp_path: Path
    ) -> None:
        recorded["validate"] = (
            '{"valid": false, "diagnostics": [{"severity": "error", '
            '"summary": "Reference to undeclared input variable", '
            '"range": {"filename": "main.tf", "start": {"line": 2}}}]}'
        )
        root = _tf(tmp_path / "repo")
        assert tofu.run([root], CIConfig(_raw={}), scratch=tmp_path / "s") == 1
        warn_cfg = CIConfig(_raw={"quality": {"tofu": "warn"}})
        assert tofu.run([root], warn_cfg, scratch=tmp_path / "s") == 0

    def test_a_timeout_is_a_finding_that_fails(
        self, recorded: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _hang(cmd: list[str], **kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

        monkeypatch.setattr(findings, "run_cmd", _hang)
        root = _tf(tmp_path / "repo")
        assert (
            tofu.run([root], CIConfig(_raw={}), scratch=tmp_path / "s", timeout=1) == 1
        )


class TestDiagnostics:
    def test_parses_path_line_and_level(self, tmp_path: Path) -> None:
        stdout = (
            '{"diagnostics": [{"severity": "warning", "summary": "Deprecated", '
            '"detail": "use y\\nmore", "range": {"filename": "a.tf", "start": {"line": 4}}}]}'
        )
        (f,) = tofu._diagnostics(stdout, tmp_path)
        assert (f.path, f.line, f.level) == (str(tmp_path / "a.tf"), 4, "warning")
        assert f.message == "Deprecated: use y"

    def test_garbage_is_no_findings(self, tmp_path: Path) -> None:
        assert tofu._diagnostics("not json", tmp_path) == []


class TestMissingTool:
    @pytest.mark.parametrize(
        ("ci", "mode", "rc"),
        [(False, "blocking", 0), (True, "blocking", 1), (True, "disabled", 0)],
    )
    def test_fails_only_a_blocking_gate_in_ci(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ci: bool,
        mode: str,
        rc: int,
    ) -> None:
        monkeypatch.setattr(tofu, "ci_binary", lambda name: None)
        _not_ci(monkeypatch)
        if ci:
            monkeypatch.setenv("CI", "true")
        cfg = CIConfig(_raw={"quality": {"tofu": mode}})
        assert tofu.run([_tf(tmp_path / "r")], cfg, scratch=tmp_path) == rc
