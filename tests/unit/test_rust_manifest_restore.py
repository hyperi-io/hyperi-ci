# Project:   HyperI CI
# File:      tests/unit/test_rust_manifest_restore.py
# Purpose:   The feature matrix puts back the manifests cargo-hack rewrites
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust._manifest import restore_cargo_manifests
from hyperi_ci.languages.rust.quality import _run_feature_matrix

MANIFEST = "hyperi_ci.languages.rust._manifest"
QUALITY = "hyperi_ci.languages.rust.quality"

ROOT = b"""[package]
name = "rootpkg"
version = "0.1.0"

[dependencies]
member = { path = "member" }

[dev-dependencies]
tempfile = "3"

[workspace]
members = ["member"]
"""

# What cargo-hack --no-dev-deps leaves while it runs.
ROOT_STRIPPED = b"""[package]
name = "rootpkg"
version = "0.1.0"

[dependencies]
member = { path = "member" }

[workspace]
members = ["member"]
"""

MEMBER = b"""[package]
name = "member"
version = "0.1.0"

[dev-dependencies]
tempfile = "3"
"""

LOCK = b"""version = 4

[[package]]
name = "rootpkg"

[[package]]
name = "tempfile"
"""

LOCK_PRUNED = b"""version = 4

[[package]]
name = "rootpkg"
"""

# An mtime no write in the test can land on.
_OLD = 1_000_000_000


class _Workspace:
    """A root package plus one member, as cargo metadata would describe it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest = root / "Cargo.toml"
        self.member = root / "member" / "Cargo.toml"
        self.lock = root / "Cargo.lock"
        self.member.parent.mkdir()
        self.manifest.write_bytes(ROOT)
        self.member.write_bytes(MEMBER)
        self.lock.write_bytes(LOCK)

    def metadata(self, *_a: Any) -> dict[str, Any]:
        return {
            "workspace_root": str(self.root),
            "packages": [
                {"name": "rootpkg", "manifest_path": str(self.manifest)},
                {"name": "member", "manifest_path": str(self.member)},
            ],
        }


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Workspace:
    monkeypatch.chdir(tmp_path)
    ws = _Workspace(tmp_path)
    monkeypatch.setattr(f"{MANIFEST}.cargo_metadata", ws.metadata)
    return ws


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(f"{MANIFEST}.info", lines.append)
    return lines


class TestRestoreCargoManifests:
    def test_changed_files_are_restored_and_unchanged_ones_left_alone(
        self, workspace: _Workspace, logged: list[str]
    ) -> None:
        os.utime(workspace.member, (_OLD, _OLD))
        with restore_cargo_manifests():
            workspace.manifest.write_bytes(ROOT_STRIPPED)
            workspace.lock.write_bytes(LOCK_PRUNED)

        assert workspace.manifest.read_bytes() == ROOT
        assert workspace.lock.read_bytes() == LOCK
        assert workspace.member.read_bytes() == MEMBER
        assert workspace.member.stat().st_mtime_ns == _OLD * 1_000_000_000
        assert len(logged) == 1
        assert "Cargo.toml" in logged[0]
        assert "Cargo.lock" in logged[0]
        assert "member" not in logged[0]

    def test_a_member_manifest_left_empty_is_restored(
        self, workspace: _Workspace
    ) -> None:
        """A cargo-hack killed mid-restore can leave a member truncated."""
        with restore_cargo_manifests():
            workspace.member.write_bytes(b"")

        assert workspace.member.read_bytes() == MEMBER

    @pytest.mark.parametrize("raised", [RuntimeError, KeyboardInterrupt])
    def test_restores_when_the_block_raises(
        self, workspace: _Workspace, raised: type[BaseException]
    ) -> None:
        with pytest.raises(raised), restore_cargo_manifests():
            workspace.manifest.write_bytes(ROOT_STRIPPED)
            workspace.member.write_bytes(b"")
            raise raised

        assert workspace.manifest.read_bytes() == ROOT
        assert workspace.member.read_bytes() == MEMBER

    def test_a_lockfile_created_inside_is_removed(
        self, workspace: _Workspace, logged: list[str]
    ) -> None:
        workspace.lock.unlink()
        with restore_cargo_manifests():
            workspace.lock.write_bytes(LOCK_PRUNED)

        assert not workspace.lock.exists()
        assert "Cargo.lock" in logged[0]

    def test_nothing_changed_logs_nothing(
        self, workspace: _Workspace, logged: list[str]
    ) -> None:
        workspace.lock.unlink()
        with restore_cargo_manifests():
            pass

        assert not workspace.lock.exists()
        assert logged == []

    def test_without_metadata_the_project_manifest_and_lock_are_kept(
        self, workspace: _Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(f"{MANIFEST}.cargo_metadata", lambda *_a: None)
        with restore_cargo_manifests():
            workspace.manifest.write_bytes(ROOT_STRIPPED)
            workspace.lock.write_bytes(LOCK_PRUNED)

        assert workspace.manifest.read_bytes() == ROOT
        assert workspace.lock.read_bytes() == LOCK

    def test_one_file_that_cannot_be_written_does_not_stop_the_rest(
        self, workspace: _Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warned: list[str] = []
        monkeypatch.setattr(f"{MANIFEST}.warn", warned.append)
        with pytest.raises(KeyboardInterrupt), restore_cargo_manifests():
            workspace.manifest.write_bytes(ROOT_STRIPPED)
            workspace.member.unlink()
            workspace.member.mkdir()
            raise KeyboardInterrupt

        assert workspace.manifest.read_bytes() == ROOT
        assert len(warned) == 1
        assert "member" in warned[0]


class _CargoHack:
    """Stands in for cargo and cargo-hack, rewriting what --no-dev-deps does."""

    def __init__(self, ws: _Workspace, interrupt: bool = False) -> None:
        self.ws = ws
        self.interrupt = interrupt
        self.seen: list[bytes] = []

    def run(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if "--no-dev-deps" in cmd:
            self.ws.manifest.write_bytes(ROOT_STRIPPED)
            self.ws.lock.write_bytes(LOCK_PRUNED)
            self.seen.append(self.ws.manifest.read_bytes())
            if self.interrupt:
                # What subprocess.run raises once it has SIGKILLed the child.
                raise KeyboardInterrupt
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch, workspace: _Workspace) -> None:
    monkeypatch.setattr(f"{QUALITY}.shutil.which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(f"{QUALITY}._has_lib_target", lambda *_a: True)
    monkeypatch.setattr(f"{QUALITY}._package_lib_map", lambda *_a: {})


def _config() -> CIConfig:
    return CIConfig(_raw={"quality": {"rust": {}}})


@pytest.mark.usefixtures("matrix")
class TestFeatureMatrixRestoresManifests:
    def test_manifests_are_back_after_the_matrix(
        self, monkeypatch: pytest.MonkeyPatch, workspace: _Workspace
    ) -> None:
        hack = _CargoHack(workspace)
        monkeypatch.setattr(f"{QUALITY}.subprocess.run", hack.run)

        assert _run_feature_matrix(_config(), workspace=True) is True

        assert hack.seen == [ROOT_STRIPPED]
        assert workspace.manifest.read_bytes() == ROOT
        assert workspace.lock.read_bytes() == LOCK

    def test_ctrl_c_during_cargo_hack_still_restores(
        self, monkeypatch: pytest.MonkeyPatch, workspace: _Workspace
    ) -> None:
        """subprocess.run kills cargo-hack 0.25s after Ctrl-C, mid-restore."""
        monkeypatch.setattr(
            f"{QUALITY}.subprocess.run", _CargoHack(workspace, interrupt=True).run
        )

        with pytest.raises(KeyboardInterrupt):
            _run_feature_matrix(_config(), workspace=True)

        assert workspace.manifest.read_bytes() == ROOT
        assert workspace.lock.read_bytes() == LOCK

    def test_a_failing_pass_still_restores(
        self, monkeypatch: pytest.MonkeyPatch, workspace: _Workspace
    ) -> None:
        hack = _CargoHack(workspace)

        def fail(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            hack.run(cmd, **kw)
            return subprocess.CompletedProcess(cmd, 101, stdout="error: boom\n")

        monkeypatch.setattr(f"{QUALITY}.subprocess.run", fail)

        assert _run_feature_matrix(_config(), workspace=True) is False

        assert workspace.manifest.read_bytes() == ROOT
        assert workspace.lock.read_bytes() == LOCK
