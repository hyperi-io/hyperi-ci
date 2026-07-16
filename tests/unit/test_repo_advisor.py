# Project:   HyperI CI
# File:      tests/unit/test_repo_advisor.py
# Purpose:   Tests for the non-blocking alint repo-hygiene advisory wrapper
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import repo_advisor


class _Config:
    """Minimal CIConfig stand-in exposing .get(key, default)."""

    def __init__(self, alint: str = "auto") -> None:
        self._alint = alint

    def get(self, key: str, default=None):
        if key == "quality.alint":
            return self._alint
        return default


def _cfg(alint: str = "auto") -> CIConfig:
    """Cast the minimal stand-in to CIConfig - the advisory only calls .get()."""
    return cast(CIConfig, _Config(alint))


def _stub_run(monkeypatch: pytest.MonkeyPatch, rc: int = 0) -> list[list[str]]:
    """Capture the command run_cmd is called with; return the capture list."""
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, *, check=True, cwd=None, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, "", "")

    monkeypatch.setattr(repo_advisor, "run_cmd", fake_run_cmd)
    return calls


def test_disabled_mode_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch)
    # find_tool must not even be consulted when disabled.
    monkeypatch.setattr(
        repo_advisor, "find_tool", lambda *a, **k: pytest.fail("should not resolve")
    )
    assert repo_advisor.run(_cfg("disabled"), Path(".")) == 0
    assert calls == []


def test_missing_alint_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch)
    monkeypatch.setattr(repo_advisor, "find_tool", lambda *a, **k: None)
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    assert calls == []  # nothing to run


def test_runs_with_packaged_config_when_no_repo_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_run(monkeypatch, rc=0)
    monkeypatch.setattr(repo_advisor, "find_tool", lambda *a, **k: "/bin/alint")
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    (cmd,) = calls
    assert cmd[:3] == ["/bin/alint", "check", "--format"]
    assert "human" in cmd  # local
    assert "-c" in cmd  # ships the HyperI default
    assert cmd[-1].endswith("hyperi.alint.yml")


def test_repo_config_wins_no_dash_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".alint.yml").write_text("version: 1\n", encoding="utf-8")
    calls = _stub_run(monkeypatch, rc=0)
    monkeypatch.setattr(repo_advisor, "find_tool", lambda *a, **k: "/bin/alint")
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    (cmd,) = calls
    assert "-c" not in cmd  # let alint discover the repo's own .alint.yml


def test_ci_uses_github_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, rc=1)  # error-level findings...
    monkeypatch.setattr(repo_advisor, "find_tool", lambda *a, **k: "/bin/alint")
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: True)
    # ...still returns 0: advisory, never gates the build.
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    (cmd,) = calls
    assert "github" in cmd


def test_exec_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Binary resolves but can't be exec'd (removed after which(), broken
    # symlink): run_cmd raises OSError - the advisory must still return 0.
    def boom(*_a, **_k):
        raise OSError("no such file")

    warns: list[str] = []
    monkeypatch.setattr(repo_advisor, "find_tool", lambda *a, **k: "/bin/alint")
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    monkeypatch.setattr(repo_advisor, "run_cmd", boom)
    monkeypatch.setattr(repo_advisor, "warn", lambda m: warns.append(m))
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    assert any("not failing" in w for w in warns)


def test_alint_internal_error_still_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run(monkeypatch, rc=2)  # config/internal error
    warns: list[str] = []
    monkeypatch.setattr(repo_advisor, "find_tool", lambda *a, **k: "/bin/alint")
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    monkeypatch.setattr(repo_advisor, "warn", lambda m: warns.append(m))
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    assert any("advisory only" in w for w in warns)
