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

import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

from hyperi_ci import bootstrap, common, versions

_TEST_ENV = {**os.environ, "HYPERCI_AUTO_UPDATE": "false"}


class TestLoadSpec:
    """The bootstrap.yaml contract."""

    def test_parses_shipped_config(self) -> None:
        rust, go_enabled = bootstrap.load_spec()

        # stable must come first -- it is the rustup default-toolchain.
        assert rust.channels[0] == "stable"
        assert "nightly" in rust.channels
        assert {"clippy", "rustfmt"} <= set(rust.components)
        assert "aarch64-unknown-linux-gnu" in rust.targets
        assert go_enabled is True


class TestNodeIsTheVersionsDefault:
    """The image bakes versions.yaml `runtimes.node` and nothing else."""

    def test_installs_one_major_and_makes_it_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ran: list[list[str]] = []
        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
        monkeypatch.setattr(bootstrap, "_have", lambda _b: False)
        monkeypatch.setattr(bootstrap, "_run", lambda cmd: ran.append(cmd) or 0)
        monkeypatch.setattr(bootstrap, "_NVM_PROFILE", tmp_path / "nvm.sh")
        monkeypatch.setenv("NVM_DIR", str(tmp_path))
        (tmp_path / "nvm.sh").write_text("", encoding="utf-8")

        assert bootstrap.install_node() == 0

        major = versions.runtime_version("node")
        [(shell, flag, script)] = ran
        assert (shell, flag) == ("bash", "-c")
        assert re.findall(r'nvm install "([^"]+)"', script) == [major]
        assert f'nvm alias default "{major}"' in script

    def test_bootstrap_yaml_lists_no_node_majors(self) -> None:
        """A second list beside versions.yaml drifts from the CI default."""
        raw = yaml.safe_load(bootstrap._CONFIG_FILE.read_text(encoding="utf-8"))
        assert "node" not in raw


def _sccache_tarball(arch: str) -> bytes:
    """Return a tarball shaped like sccache's release asset for ``arch``."""
    version = versions.tool_version("sccache")
    body = b"#!/bin/sh\necho sccache\n"
    member = tarfile.TarInfo(f"sccache-{version}-{arch}-unknown-linux-musl/sccache")
    member.size = len(body)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        archive.addfile(member, io.BytesIO(body))
    return buf.getvalue()


class TestNothingUnpinned:
    """Every tool the bake fetches is a versions.yaml pin, checked by digest."""

    @staticmethod
    def _wire(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: bytes | None
    ) -> dict[str, list]:
        seen: dict[str, list] = {"fetch": [], "run": []}

        def fake_fetch(name: str, url: str, sha: str | None = None) -> bytes | None:
            seen["fetch"].append((name, url, sha))
            return payload

        def fake_run(cmd: list[str], **_kw: object) -> int:
            seen["run"].append(cmd)
            return 0

        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
        monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(bootstrap, "_have", lambda _b: True)
        monkeypatch.setattr(bootstrap, "fetch_verified", fake_fetch)
        monkeypatch.setattr(bootstrap, "_run", fake_run)
        monkeypatch.setenv("CARGO_HOME", str(tmp_path))
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        return seen

    def test_rust_bake_fetches_only_the_pinned_sccache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen = self._wire(monkeypatch, tmp_path, _sccache_tarball("x86_64"))
        rust, _ = bootstrap.load_spec()

        assert bootstrap.install_rust(rust) == 0

        version = versions.tool_version("sccache")
        [(name, url, sha)] = seen["fetch"]
        assert name == "sccache"
        assert f"/releases/download/{version}/" in url
        assert url.endswith(f"sccache-{version}-x86_64-unknown-linux-musl.tar.gz")
        assert sha == versions.tool_sha256("sccache", "x86_64")
        # No cargo install of any kind: those carry no version and no digest.
        assert not [c for c in seen["run"] if c[0] == "cargo"]
        binary = tmp_path / "bin" / "sccache"
        assert binary.read_bytes() == b"#!/bin/sh\necho sccache\n"
        assert os.access(binary, os.X_OK)

    def test_arm64_takes_the_aarch64_pin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen = self._wire(monkeypatch, tmp_path, _sccache_tarball("aarch64"))
        monkeypatch.setattr(bootstrap.platform, "machine", lambda: "arm64")

        assert bootstrap.install_sccache(tmp_path) == 0
        [(_, url, sha)] = seen["fetch"]
        assert "aarch64-unknown-linux-musl" in url
        assert sha == versions.tool_sha256("sccache", "aarch64")

    def test_digest_mismatch_installs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """fetch_verified returns None on a mismatch, and the bake must fail."""
        self._wire(monkeypatch, tmp_path, None)

        assert bootstrap.install_sccache(tmp_path) == 1
        assert not (tmp_path / "sccache").exists()

    def test_tarball_without_the_binary_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path, _sccache_tarball("aarch64"))

        assert bootstrap.install_sccache(tmp_path) == 1
        assert not (tmp_path / "sccache").exists()

    def test_unknown_cpu_fetches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen = self._wire(monkeypatch, tmp_path, _sccache_tarball("x86_64"))
        monkeypatch.setattr(bootstrap.platform, "machine", lambda: "riscv64")

        assert bootstrap.install_sccache(tmp_path) == 1
        assert seen["fetch"] == []

    def test_no_url_tracks_a_branch(self) -> None:
        """A branch moves under the image build; a release tag and digest do not."""
        source = Path(bootstrap.__file__).read_text(encoding="utf-8")
        urls = re.findall(r"https?://[^\s\"']+", source)
        assert urls, "no URL found in bootstrap.py - the scan broke"
        assert not [u for u in urls if re.search(r"/(main|master|HEAD)/", u)]


class TestNonLinuxGuard:
    """Every install path no-ops off Linux rather than half-running."""

    @pytest.mark.skipif(
        sys.platform.startswith("linux"),
        reason="asserts the non-Linux branch; on Linux these really install",
    )
    def test_all_installers_noop(self) -> None:
        rust, _ = bootstrap.load_spec()
        assert bootstrap.install_rust(rust) == 0
        assert bootstrap.install_go() == 0
        assert bootstrap.install_node() == 0
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
        monkeypatch.setattr(bootstrap, "install_sccache", lambda _bin: 0)
        seen = self._wire(monkeypatch, b"#!/bin/sh\necho rustup\n")

        assert bootstrap.install_rust(bootstrap.RustSpec(channels=[])) == 0

        [curl] = seen["curl"]
        assert curl[-1] == bootstrap._RUSTUP_URL
        assert "-L" not in curl
        assert "--max-time" in curl
        assert curl[curl.index("--proto") + 1] == "=https"
        assert "--tlsv1.2" in curl
        [(shell, body)] = seen["shell"]
        assert shell[:2] == ["sh", "-s"]
        assert body == b"#!/bin/sh\necho rustup\n"


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
        assert f"sccache:     {versions.tool_version('sccache')}" in combined
        assert f"node: {versions.runtime_version('node')}\n" in combined

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
