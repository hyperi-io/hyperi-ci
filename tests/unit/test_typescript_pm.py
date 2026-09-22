# Project:   HyperI CI
# File:      tests/unit/test_typescript_pm.py
# Purpose:   Package-manager resolution honours the Corepack pin
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A packageManager pin must resolve through Corepack, not a global binary.

On GitHub-hosted runners the global yarn is 1.22 while projects pin yarn 4.x;
without Corepack the global binary refuses to run the project at all, so
"on PATH" is not "usable for this project".
"""

from __future__ import annotations

import json
from pathlib import Path

from hyperi_ci.languages.typescript import _common
from hyperi_ci.languages.typescript._common import (
    detect_package_manager,
    ensure_pm_available,
    pinned_package_manager,
)


def _project(tmp_path: Path, package_manager: str | None) -> Path:
    pkg: dict[str, object] = {"name": "t"}
    if package_manager:
        pkg["packageManager"] = package_manager
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    return tmp_path


def test_a_pin_is_read_from_package_json(tmp_path):
    root = _project(tmp_path, "yarn@4.13.0")
    assert pinned_package_manager(root) == "yarn"
    assert detect_package_manager(root) == "yarn"


def test_no_pin_reads_as_none(tmp_path):
    root = _project(tmp_path, None)
    assert pinned_package_manager(root) is None


def test_unpinned_project_accepts_a_global_binary(tmp_path, monkeypatch):
    root = _project(tmp_path, None)
    (root / "yarn.lock").write_text("")
    monkeypatch.setattr(_common.shutil, "which", lambda _pm: "/usr/local/bin/yarn")
    enabled = []
    monkeypatch.setattr(_common, "_corepack_enable", lambda: enabled.append(1) or True)
    assert ensure_pm_available("yarn", root) is True
    assert enabled == []


def test_a_pinned_project_does_not_trust_the_global_binary(tmp_path, monkeypatch):
    root = _project(tmp_path, "yarn@4.13.0")
    monkeypatch.setattr(_common.shutil, "which", lambda _pm: "/usr/local/bin/yarn")
    monkeypatch.setattr(_common.Path, "home", lambda: tmp_path / "nohome")
    enabled = []
    monkeypatch.setattr(_common, "_corepack_enable", lambda: enabled.append(1) or True)
    assert ensure_pm_available("yarn", root) is True
    assert enabled == [1]


def test_a_pinned_project_accepts_an_existing_corepack_shim(tmp_path, monkeypatch):
    root = _project(tmp_path, "yarn@4.13.0")
    home = tmp_path / "home"
    shim_dir = home / ".corepack" / "bin"
    shim_dir.mkdir(parents=True)
    monkeypatch.setattr(_common.Path, "home", lambda: home)
    monkeypatch.setattr(_common.shutil, "which", lambda _pm: str(shim_dir / "yarn"))
    enabled = []
    monkeypatch.setattr(_common, "_corepack_enable", lambda: enabled.append(1) or True)
    assert ensure_pm_available("yarn", root) is True
    assert enabled == []


def test_without_corepack_a_global_binary_is_the_honest_fallback(tmp_path, monkeypatch):
    root = _project(tmp_path, "yarn@4.13.0")
    monkeypatch.setattr(_common.Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setattr(_common.shutil, "which", lambda _pm: "/usr/local/bin/yarn")
    monkeypatch.setattr(_common, "_corepack_enable", lambda: False)
    assert ensure_pm_available("yarn", root) is True
