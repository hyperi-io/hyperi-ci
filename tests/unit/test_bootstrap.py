# Project:   HyperI CI
# File:      tests/unit/test_bootstrap.py
# Purpose:   Tests for the runner-image language toolchain bootstrap
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.bootstrap`.

The install paths themselves are Linux-only and shell out to rustup / go.dev /
nvm, so they are exercised for real by a runner image build, not here. What is
tested here is everything that can be checked without a Linux box: the config
contract, the non-Linux guard, and the CLI wiring.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyperi_ci import bootstrap, common

_TEST_ENV = {**os.environ, "HYPERCI_AUTO_UPDATE": "false"}


class TestLoadSpec:
    """The bootstrap.yaml contract."""

    def test_parses_shipped_config(self) -> None:
        rust, go_enabled, node = bootstrap.load_spec()

        # stable must come first -- it is the rustup default-toolchain.
        assert rust.channels[0] == "stable"
        assert "nightly" in rust.channels
        assert {"clippy", "rustfmt"} <= set(rust.components)
        assert "aarch64-unknown-linux-gnu" in rust.targets
        assert go_enabled is True
        assert node.default in node.versions

    def test_cargo_tools_are_the_expensive_source_builds(self) -> None:
        """These four are why the image exists -- they compile from source.

        If one is dropped from the config, jobs pay that build cost per run,
        which is the regression the pre-built image was created to avoid.
        """
        rust, _, _ = bootstrap.load_spec()
        assert {"sccache", "cargo-audit", "cargo-deny", "cargo-nextest"} <= set(
            rust.cargo_tools
        )


class TestNonLinuxGuard:
    """Every install path no-ops off Linux rather than half-running."""

    @pytest.mark.skipif(
        sys.platform.startswith("linux"),
        reason="asserts the non-Linux branch; on Linux these really install",
    )
    def test_all_installers_noop(self) -> None:
        rust, _, node = bootstrap.load_spec()
        assert bootstrap.install_rust(rust) == 0
        assert bootstrap.install_go() == 0
        assert bootstrap.install_node(node) == 0
        assert bootstrap.install_toolchain_bootstrap() == 0

    def test_sudo_prefix_empty_off_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Darwin")
        assert bootstrap._sudo_prefix() == []

    def test_sudo_prefix_empty_as_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Dockerfile RUN is root with no sudo configured."""
        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
        monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
        assert bootstrap._sudo_prefix() == []

    def test_sudo_prefix_used_when_non_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
        monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 1001)
        assert bootstrap._sudo_prefix() == ["sudo"]


class TestInstallerScriptsComeFromAFile:
    """A retried fetch piped straight into a shell can run its body twice.

    The installer scripts are fetched to a temp file with retries, then the
    whole file goes to the shell.
    """

    @staticmethod
    def _wire(monkeypatch: pytest.MonkeyPatch, script: bytes) -> dict[str, list]:
        seen: dict[str, list] = {"curl": [], "shell": []}

        def fake_curl(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
            seen["curl"].append(cmd)
            Path(cmd[cmd.index("-o") + 1]).write_bytes(script)
            return subprocess.CompletedProcess(cmd, 0)

        def fake_shell(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
            seen["shell"].append((cmd, kw.get("input")))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(common, "run_cmd", fake_curl)
        monkeypatch.setattr(bootstrap.subprocess, "run", fake_shell)
        monkeypatch.setattr(bootstrap, "_run", lambda *_a, **_k: 0)
        return seen

    def test_rustup_keeps_its_own_transport_rules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
        monkeypatch.setattr(bootstrap, "_have", lambda _b: False)
        monkeypatch.setenv("CARGO_HOME", str(tmp_path))
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        seen = self._wire(monkeypatch, b"#!/bin/sh\necho rustup\n")

        assert bootstrap.install_rust(bootstrap.RustSpec(channels=[])) == 0

        [curl] = seen["curl"]
        assert curl[-1] == bootstrap._RUSTUP_URL
        assert "-L" not in curl
        assert "--retry-all-errors" in curl
        assert curl[curl.index("--proto") + 1] == "=https"
        assert "--tlsv1.2" in curl
        [(shell, body)] = seen["shell"]
        assert shell[:2] == ["sh", "-s"]
        assert body == b"#!/bin/sh\necho rustup\n"

    def test_cargo_binstall_script_is_piped_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bootstrap, "_have", lambda _b: False)
        seen = self._wire(monkeypatch, b"#!/bin/bash\necho binstall\n")

        assert bootstrap._install_cargo_tools(["sccache"]) == 0

        [curl] = seen["curl"]
        assert curl[-1] == bootstrap._CARGO_BINSTALL_URL
        assert "--retry-all-errors" in curl
        assert seen["shell"] == [(["bash"], b"#!/bin/bash\necho binstall\n")]


class TestInstallAllWiring:
    """install-all covers toolchains as well as apt deps."""

    def _run(self, *args: str, cwd) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "hyperi_ci.cli", "install-all", *args],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=_TEST_ENV,
        )

    def test_dry_run_includes_toolchain_plan(self, tmp_path) -> None:
        result = self._run("--dry-run", cwd=tmp_path)
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "language toolchains" in combined
        assert "cargo tools:" in combined
        assert "sccache" in combined

    def test_toolchains_planned_before_apt_deps(self, tmp_path) -> None:
        """Ordering matters: the apt families include BOLT and the
        cross-compilers a Rust build then links against."""
        result = self._run("--dry-run", cwd=tmp_path)
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert combined.index("language toolchains") < combined.index(
            "native-deps/rust"
        )

    def test_skip_toolchains_excludes_them(self, tmp_path) -> None:
        result = self._run("--dry-run", "--skip-toolchains", cwd=tmp_path)
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "language toolchains" not in combined
        # the apt side still runs
        assert "native-deps/rust" in combined
