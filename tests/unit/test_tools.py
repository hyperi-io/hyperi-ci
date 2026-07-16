# Project:   HyperI CI
# File:      tests/unit/test_tools.py
# Purpose:   Tests for the external-tool presence + guidance abstraction
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
from __future__ import annotations

import shutil

import pytest

from hyperi_ci import tools


def test_registry_has_core_tools() -> None:
    for name in ("alint", "gitleaks", "semgrep", "osv-scanner", "gh", "helm", "aws"):
        assert tools.tool_info(name) is not None, name


def test_notice_for_known_tool_is_actionable() -> None:
    notice = tools.missing_tool_notice("alint")
    assert "`alint` is not installed" in notice
    assert "hyperi-ci needs it for" in notice  # names the purpose
    assert "help: install it with one of:" in notice  # tells you HOW to fix
    assert "cargo install alint" in notice  # a real install command
    assert "https://github.com/asamarts/alint" in notice  # docs URL


def test_notice_for_unknown_tool_is_generic_but_safe() -> None:
    notice = tools.missing_tool_notice("frobnicate")
    assert "`frobnicate` is not installed" in notice
    # No registry entry -> no purpose/install/url lines, but still a clean line.
    assert "help:" not in notice
    assert "docs:" not in notice


def test_notice_overrides_win() -> None:
    notice = tools.missing_tool_notice(
        "alint",
        purpose="a custom purpose",
        install=["do-the-thing"],
        url="https://example.test",
    )
    assert "a custom purpose" in notice
    assert "do-the-thing" in notice
    assert "https://example.test" in notice
    # registry defaults are replaced, not appended
    assert "cargo install alint" not in notice


def test_find_tool_returns_path_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert tools.find_tool("gitleaks") == "/usr/bin/gitleaks"


def test_find_tool_optional_emits_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    infos: list[str] = []
    warns: list[str] = []
    monkeypatch.setattr(tools, "info", lambda m: infos.append(m))
    monkeypatch.setattr(tools, "warn", lambda m: warns.append(m))
    assert tools.find_tool("alint") is None
    assert len(infos) == 1 and not warns  # optional -> info, not warn
    assert "cargo install alint" in infos[0]


def test_find_tool_recommended_emits_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    infos: list[str] = []
    warns: list[str] = []
    monkeypatch.setattr(tools, "info", lambda m: infos.append(m))
    monkeypatch.setattr(tools, "warn", lambda m: warns.append(m))
    assert tools.find_tool("gitleaks", recommended=True) is None
    assert len(warns) == 1 and not infos  # recommended -> warn
