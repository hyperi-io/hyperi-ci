# Project:   HyperI CI
# File:      tests/unit/test_install_test_tools.py
# Purpose:   Tests for scripts/install-test-tools.py's $GITHUB_PATH handling
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "install_test_tools",
    Path(__file__).resolve().parents[2] / "scripts" / "install-test-tools.py",
)
assert _SPEC is not None and _SPEC.loader is not None
itt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(itt)


def test_only_cache_installs_reach_github_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    installed = cache / "native-tools" / "alint" / "v1-amd64-0123456789ab"
    found = {"lychee": "/usr/bin/lychee", "alint": str(installed / "alint")}
    github_path = tmp_path / "github_path"
    monkeypatch.setenv("GITHUB_PATH", str(github_path))
    monkeypatch.setattr(itt, "install_root", lambda: cache / "native-tools")
    monkeypatch.setattr(itt, "ci_binary", found.get)

    assert itt.main() == 0
    assert github_path.read_text(encoding="utf-8") == f"{installed}\n"


def test_nothing_installed_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_path = tmp_path / "github_path"
    monkeypatch.setenv("GITHUB_PATH", str(github_path))
    monkeypatch.setattr(itt, "install_root", lambda: tmp_path / "native-tools")
    monkeypatch.setattr(itt, "ci_binary", lambda name: f"/usr/bin/{name}")

    assert itt.main() == 0
    assert not github_path.exists()


def test_a_failed_install_fails_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(itt, "ci_binary", lambda _name: None)
    assert itt.main() == 1
