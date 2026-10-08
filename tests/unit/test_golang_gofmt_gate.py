# Project:   HyperI CI
# File:      tests/unit/test_golang_gofmt_gate.py
# Purpose:   Tests that the gofmt gate fails on files gofmt -l lists
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``gofmt -l`` exits 0 while listing unformatted files, so the list is the finding."""

import shutil
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages import quality_common
from hyperi_ci.languages.golang import quality as go_quality

pytestmark = pytest.mark.skipif(
    shutil.which("gofmt") is None, reason="gofmt not installed"
)

_UNFORMATTED = "package demo\nfunc  Demo( ) {}\n"
_FORMATTED = "package demo\n\nfunc Demo() {}\n"


class _Said:
    """Every line the handler emits, by level."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.errors: list[str] = []
        self.warns: list[str] = []
        self.infos: list[str] = []
        for module in (go_quality, quality_common):
            monkeypatch.setattr(module, "error", self.errors.append, raising=False)
            monkeypatch.setattr(module, "warn", self.warns.append)
            monkeypatch.setattr(module, "info", self.infos.append)


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Said:
    """Run only gofmt, on a dev box, from an empty Go tree."""
    monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("HYPERCI_QUALITY_SKIP", "govet,golangci_lint,gosec,govulncheck")
    monkeypatch.chdir(tmp_path)
    return _Said(monkeypatch)


def _config(mode: str) -> CIConfig:
    return CIConfig(_raw={"quality": {"golang": {"gofmt": mode}}})


def test_unformatted_file_fails_a_blocking_gate(said: _Said, tmp_path: Path) -> None:
    (tmp_path / "demo.go").write_text(_UNFORMATTED, encoding="utf-8")

    assert go_quality.run(_config("blocking")) == 1
    assert any("gofmt: failed" in e for e in said.errors), said.errors
    assert any("demo.go" in i for i in said.infos), said.infos


def test_unformatted_file_only_warns_under_warn(said: _Said, tmp_path: Path) -> None:
    (tmp_path / "demo.go").write_text(_UNFORMATTED, encoding="utf-8")

    assert go_quality.run(_config("warn")) == 0
    assert any("gofmt: issues found" in w for w in said.warns), said.warns
    assert any("demo.go" in i for i in said.infos), said.infos
    assert said.errors == []


def test_formatted_tree_passes_a_blocking_gate(said: _Said, tmp_path: Path) -> None:
    (tmp_path / "demo.go").write_text(_FORMATTED, encoding="utf-8")

    assert go_quality.run(_config("blocking")) == 0
    assert said.errors == []


def test_disabled_gate_does_not_look(said: _Said, tmp_path: Path) -> None:
    (tmp_path / "demo.go").write_text(_UNFORMATTED, encoding="utf-8")

    assert go_quality.run(_config("disabled")) == 0
    assert any("gofmt: disabled" in i for i in said.infos), said.infos
