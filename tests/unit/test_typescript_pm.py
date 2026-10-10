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

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.languages.typescript import _common
from hyperi_ci.languages.typescript._common import (
    detect_package_manager,
    ensure_pm_available,
    package_scripts,
    pinned_package_manager,
    read_package_json,
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


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[1, 2]", '"a string"'],
    ids=["empty", "invalid", "array", "string"],
)
def test_an_unusable_manifest_reads_as_empty(tmp_path, text):
    (tmp_path / "package.json").write_text(text)
    assert read_package_json(tmp_path) == {}
    assert package_scripts(tmp_path) == {}
    assert pinned_package_manager(tmp_path) is None


def test_a_missing_manifest_reads_as_empty(tmp_path):
    assert read_package_json(tmp_path) == {}


def test_scripts_that_are_not_a_table_read_as_empty(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": "lint"}))
    assert package_scripts(tmp_path) == {}


def test_the_first_defined_candidate_script_wins(tmp_path, monkeypatch):
    from hyperi_ci.languages.typescript import quality

    monkeypatch.chdir(tmp_path)
    scripts = {"check:format": "x", "check-format": "y"}
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    found = quality._find_npm_script(["format:check", "check-format", "check:format"])
    assert found == "check-format"
    assert quality._find_npm_script(["lint"]) is None


class TestCorepackEnable:
    """Corepack writes shims beside its binary, which a runner may own as root."""

    @staticmethod
    def _setup(monkeypatch, tmp_path: Path, *, writable: bool, rcs: list[int]):
        node_bin = tmp_path / "node" / "bin"
        node_bin.mkdir(parents=True)
        monkeypatch.setattr(
            _common.shutil, "which", lambda _name: str(node_bin / "corepack")
        )
        monkeypatch.setattr(_common.os, "access", lambda _p, _m: writable)
        monkeypatch.setattr(_common.Path, "home", lambda: tmp_path / "home")
        monkeypatch.setenv("PATH", "/usr/bin")
        calls: list[tuple[str, ...]] = []

        def _run(*args: str):
            calls.append(args)
            return SimpleNamespace(returncode=rcs.pop(0), stderr="")

        monkeypatch.setattr(_common, "_run_corepack_enable", _run)
        warned: list[str] = []
        monkeypatch.setattr(_common, "warn", warned.append)
        return calls, warned

    def test_a_read_only_bin_goes_straight_to_the_user_directory(
        self, monkeypatch, tmp_path
    ):
        calls, warned = self._setup(monkeypatch, tmp_path, writable=False, rcs=[0])
        assert _common._corepack_enable() is True
        assert calls == [
            ("--install-directory", str(tmp_path / "home" / ".corepack" / "bin"))
        ]
        assert warned == []
        assert _common.os.environ["PATH"].startswith(
            str(tmp_path / "home" / ".corepack" / "bin")
        )

    def test_a_writable_bin_enables_in_place(self, monkeypatch, tmp_path):
        calls, warned = self._setup(monkeypatch, tmp_path, writable=True, rcs=[0])
        assert _common._corepack_enable() is True
        assert calls == [()]
        assert warned == []

    def test_an_unexpected_failure_still_warns_and_retries(self, monkeypatch, tmp_path):
        calls, warned = self._setup(monkeypatch, tmp_path, writable=True, rcs=[1, 0])
        assert _common._corepack_enable() is True
        assert len(calls) == 2
        assert "retrying with user directory" in warned[0]
