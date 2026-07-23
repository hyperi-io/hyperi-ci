# Project:   HyperI CI
# File:      tests/unit/test_install.py
# Purpose:   Tests for the shared CI-binary installer
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.install.install_ci_binary (no real downloads)."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from hyperi_ci.quality import install


def _make_targz(member_name: str, data: bytes = b"BINARY") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=member_name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestInstallCiBinary:
    def test_returns_existing_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(install.shutil, "which", lambda n: "/usr/bin/hadolint")
        assert install.install_ci_binary("hadolint", "http://x") == "/usr/bin/hadolint"

    def test_none_off_ci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(install.shutil, "which", lambda n: None)
        monkeypatch.setattr(install, "is_ci", lambda: False)
        assert install.install_ci_binary("hadolint", "http://x") is None

    def test_none_off_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(install.shutil, "which", lambda n: None)
        monkeypatch.setattr(install, "is_ci", lambda: True)
        monkeypatch.setattr(install.sys, "platform", "darwin")
        assert install.install_ci_binary("hadolint", "http://x") is None


class TestInstallBody:
    """The download/extract/install core - Linux CI, all subprocess mocked."""

    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        curl_stdout: bytes = b"BIN",
        curl_rc: int = 0,
        sudo_raises: bool = False,
    ) -> dict:
        monkeypatch.setattr(install, "is_ci", lambda: True)
        monkeypatch.setattr(install.sys, "platform", "linux")
        state: dict = {"installed": False, "cmds": []}
        monkeypatch.setattr(
            install.shutil,
            "which",
            lambda n: "/usr/local/bin/tool" if state["installed"] else None,
        )

        def _run(cmd, **kw):  # noqa: ANN001, ANN003
            state["cmds"].append(cmd)
            if cmd[0] == "curl":
                return SimpleNamespace(returncode=curl_rc, stdout=curl_stdout)
            if sudo_raises:
                raise subprocess.CalledProcessError(1, cmd)
            if cmd[:2] == ["sudo", "mv"]:
                state["installed"] = True
            return SimpleNamespace(returncode=0, stdout=b"")

        monkeypatch.setattr(install.subprocess, "run", _run)
        return state

    def test_raw_binary_success_uses_f_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = self._wire(monkeypatch)
        assert install.install_ci_binary("tool", "http://x") == "/usr/local/bin/tool"
        # curl -f: fail on HTTP error instead of saving a 404 page as the binary.
        assert any(c[0] == "curl" and "-fsSL" in c for c in state["cmds"])

    def test_tar_member_extracted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire(monkeypatch, curl_stdout=_make_targz("tool"))
        assert (
            install.install_ci_binary("tool", "http://x", tar_member="tool")
            == "/usr/local/bin/tool"
        )

    def test_tar_member_missing_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._wire(monkeypatch, curl_stdout=_make_targz("other"))
        assert install.install_ci_binary("tool", "http://x", tar_member="tool") is None

    def test_curl_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire(monkeypatch, curl_rc=1)
        assert install.install_ci_binary("tool", "http://x") is None

    def test_sudo_failure_returns_none_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A hardened runner without passwordless sudo must yield None, not a
        # CalledProcessError crashing the whole quality stage.
        self._wire(monkeypatch, sudo_raises=True)
        assert install.install_ci_binary("tool", "http://x") is None

    # --- fail-closed SHA256 integrity gate (the estate-wide RCE fix) ----------

    def test_wrong_hash_installs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A swapped asset (its bytes hash to something other than the pin) is
        # the estate-wide RCE case: refuse it, install nothing.
        state = self._wire(monkeypatch, curl_stdout=b"EVIL")
        result = install.install_ci_binary(
            "tool", "http://x", expected_sha256="00" * 32
        )
        assert result is None
        assert state["installed"] is False
        # Never reached the privileged move - the gate is before any exec.
        assert not any(c[:2] == ["sudo", "mv"] for c in state["cmds"])

    def test_matching_hash_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b"GOOD-BINARY"
        state = self._wire(monkeypatch, curl_stdout=payload)
        digest = hashlib.sha256(payload).hexdigest()
        result = install.install_ci_binary("tool", "http://x", expected_sha256=digest)
        assert result == "/usr/local/bin/tool"
        assert state["installed"] is True

    def test_matching_hash_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = b"GOOD-BINARY"
        self._wire(monkeypatch, curl_stdout=payload)
        digest = hashlib.sha256(payload).hexdigest().upper()
        assert (
            install.install_ci_binary("tool", "http://x", expected_sha256=digest)
            == "/usr/local/bin/tool"
        )

    def test_tar_download_hashed_before_extract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # For a tar member the RAW .tar.gz is what gets hashed (before extract),
        # so the pin is the archive digest and a wrong pin fails closed.
        archive = _make_targz("tool")
        state = self._wire(monkeypatch, curl_stdout=archive)
        result = install.install_ci_binary(
            "tool", "http://x", tar_member="tool", expected_sha256="00" * 32
        )
        assert result is None
        assert state["installed"] is False

    def test_tar_matching_archive_hash_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = _make_targz("tool")
        self._wire(monkeypatch, curl_stdout=archive)
        digest = hashlib.sha256(archive).hexdigest()
        assert (
            install.install_ci_binary(
                "tool", "http://x", tar_member="tool", expected_sha256=digest
            )
            == "/usr/local/bin/tool"
        )

    def test_no_hash_still_installs_unverified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rollout affordance: omitting the pin warns but proceeds, so callers
        # that have not pinned a hash yet keep working.
        state = self._wire(monkeypatch, curl_stdout=b"BIN")
        assert (
            install.install_ci_binary("tool", "http://x", expected_sha256=None)
            == "/usr/local/bin/tool"
        )
        assert state["installed"] is True
