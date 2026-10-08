# Project:   HyperI CI
# File:      tests/unit/test_bootstrap.py
# Purpose:   Tests for the runner-image language toolchain bootstrap
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.bootstrap`.

The installers download and run vendor code, so they are exercised for real by
a runner image build. Here curl is faked at `common.run_cmd`: what is checked is
which URLs are fetched, that each download is held to its versions.yaml digest,
the config contract, the non-Linux guard and the CLI wiring.
"""

import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from hyperi_ci import bootstrap, common, native_tools, versions

_TEST_ENV = {**os.environ, "HYPERCI_AUTO_UPDATE": "false"}
API_HOST = "api.github.com"


def _hosts(urls: list[str]) -> set[str | None]:
    return {urlparse(u).hostname for u in urls}


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


class TestPythonIsTheVersionsDefault:
    """The image bakes versions.yaml `runtimes.python` through uv."""

    @staticmethod
    def _linux(monkeypatch: pytest.MonkeyPatch, *, uv: bool = True) -> None:
        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
        monkeypatch.setattr(bootstrap, "_have", lambda b: uv and b == "uv")

    def test_argv_is_uv_python_install_with_the_versions_yaml_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[list[str]] = []
        self._linux(monkeypatch)
        monkeypatch.setattr(bootstrap, "_run", lambda cmd: ran.append(cmd) or 0)

        assert bootstrap.install_python() == 0

        assert ran == [["uv", "python", "install", versions.runtime_version("python")]]

    def test_version_follows_versions_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[list[str]] = []
        self._linux(monkeypatch)
        monkeypatch.setattr(bootstrap, "_run", lambda cmd: ran.append(cmd) or 0)
        monkeypatch.setattr(bootstrap, "runtime_version", lambda _name: "9.99")

        assert bootstrap.install_python() == 0

        assert ran == [["uv", "python", "install", "9.99"]]

    def test_install_dir_env_is_passed_through_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: list[tuple[list[str], dict]] = []

        def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
            seen.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0)

        self._linux(monkeypatch)
        monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
        monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(tmp_path))

        assert bootstrap.install_python() == 0

        [(cmd, kw)] = seen
        assert "--install-dir" not in cmd
        # None inherits this process's environment unchanged.
        assert kw.get("env") is None
        assert os.environ["UV_PYTHON_INSTALL_DIR"] == str(tmp_path)

    def test_never_sets_the_install_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._linux(monkeypatch)
        monkeypatch.setattr(bootstrap, "_run", lambda _cmd: 0)
        monkeypatch.delenv("UV_PYTHON_INSTALL_DIR", raising=False)

        assert bootstrap.install_python() == 0

        assert "UV_PYTHON_INSTALL_DIR" not in os.environ

    def test_uv_failure_propagates_its_exit_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._linux(monkeypatch)
        monkeypatch.setattr(bootstrap, "_run", lambda _cmd: 2)

        assert bootstrap.install_python() == 2

    def test_missing_uv_fails_without_running_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[list[str]] = []
        self._linux(monkeypatch, uv=False)
        monkeypatch.setattr(bootstrap, "_run", lambda cmd: ran.append(cmd) or 0)

        assert bootstrap.install_python() == 1
        assert ran == []

    def test_skipped_off_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ran: list[list[str]] = []
        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(bootstrap, "_run", lambda cmd: ran.append(cmd) or 0)

        assert bootstrap.install_python() == 0
        assert ran == []


def _sccache_tarball(arch: str, file: str = "sccache") -> bytes:
    """Return a tarball shaped like sccache's release asset for ``arch``."""
    version = versions.tool_version("sccache")
    body = b"#!/bin/sh\necho sccache\n"
    member = tarfile.TarInfo(f"sccache-{version}-{arch}-unknown-linux-musl/{file}")
    member.size = len(body)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        archive.addfile(member, io.BytesIO(body))
    return buf.getvalue()


class TestNothingUnpinned:
    """Every tool the bake fetches is a versions.yaml pin, checked by digest."""

    @staticmethod
    def _wire(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: bytes
    ) -> dict[str, list]:
        seen: dict[str, list] = {"fetch": [], "digest": [], "run": []}

        def fake_download(name: str, url: str) -> bytes:
            seen["fetch"].append((name, url))
            return payload

        def fake_digest(name: str, key: str) -> str:
            seen["digest"].append((name, key))
            return hashlib.sha256(payload).hexdigest()

        def fake_run(cmd: list[str], **_kw: object) -> int:
            seen["run"].append(cmd)
            return 0

        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
        monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(native_tools.sys, "platform", "linux")
        monkeypatch.setattr(bootstrap, "_have", lambda _b: True)
        monkeypatch.setattr(native_tools, "download_artefact", fake_download)
        monkeypatch.setattr(native_tools, "tool_sha256", fake_digest)
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
        [(name, url)] = seen["fetch"]
        assert name == "sccache"
        assert f"/releases/download/{version}/" in url
        assert url.endswith(f"sccache-{version}-x86_64-unknown-linux-musl.tar.gz")
        assert seen["digest"] == [("sccache", "x86_64")]
        assert versions.tool_sha256("sccache", "x86_64")
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
        [(_, url)] = seen["fetch"]
        assert "aarch64-unknown-linux-musl" in url
        assert seen["digest"] == [("sccache", "aarch64")]
        assert versions.tool_sha256("sccache", "aarch64")

    def test_digest_mismatch_installs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path, _sccache_tarball("x86_64"))
        monkeypatch.setattr(native_tools, "tool_sha256", lambda n, k: "0" * 64)

        assert bootstrap.install_sccache(tmp_path) == 1
        assert not (tmp_path / "sccache").exists()

    def test_tarball_without_the_binary_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path, _sccache_tarball("x86_64", "README.md"))

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


class TestInstallersArePinnedAndVerified:
    """rustup-init, the Go tarball and nvm's install.sh: pinned, digest-checked.

    curl is faked at `common.run_cmd`, so every URL the installers fetch is
    recorded, and the download is written to the file curl was given. Each
    installer runs from that file, never from piped stdin, because a retried
    fetch piped into a shell can run its body twice.
    """

    @staticmethod
    def _wire(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: bytes
    ) -> dict[str, list]:
        seen: dict[str, list] = {"curl": [], "run": []}

        def fake_curl(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
            seen["curl"].append(cmd)
            Path(cmd[cmd.index("-o") + 1]).write_bytes(payload)
            return subprocess.CompletedProcess(cmd, 0, stdout="200")

        def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
            seen["run"].append((cmd, kw.get("env"), kw.get("input")))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(common, "run_cmd", fake_curl)
        monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
        monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(native_tools.sys, "platform", "linux")
        monkeypatch.setattr(bootstrap, "_GO_PARENT", tmp_path)
        monkeypatch.setattr(bootstrap, "sudo_prefix", lambda: [])
        return seen

    @staticmethod
    def _pin(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
        """Make every pinned digest the payload's, so the download verifies."""
        digest = hashlib.sha256(payload).hexdigest()
        monkeypatch.setattr(bootstrap, "tool_sha256", lambda _n, _k: digest)
        monkeypatch.setattr(bootstrap, "runtime_sha256", lambda _n, _k: digest)

    @staticmethod
    def _urls(seen: dict[str, list]) -> list[str]:
        return [cmd[-1] for cmd in seen["curl"]]

    # rustup

    def test_rustup_init_is_the_pinned_version_with_rustups_transport_rules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        payload = b"\x7fELF rustup-init"
        seen = self._wire(monkeypatch, tmp_path, payload)
        self._pin(monkeypatch, payload)

        assert bootstrap.install_rustup("stable") == 0

        version = versions.tool_version("rustup")
        [curl] = seen["curl"]
        assert curl[-1] == (
            f"https://static.rust-lang.org/rustup/archive/{version}/"
            "x86_64-unknown-linux-gnu/rustup-init"
        )
        assert "-L" not in curl
        assert curl[curl.index("--proto") + 1] == "=https"
        assert "--tlsv1.2" in curl
        [(cmd, _env, stdin)] = seen["run"]
        assert Path(cmd[0]).name == "rustup-init"
        assert cmd[1:] == ["-y", "--default-toolchain", "stable"]
        assert stdin is None

    def test_rustup_arm64_takes_the_aarch64_pin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        payload = b"\x7fELF rustup-init"
        seen = self._wire(monkeypatch, tmp_path, payload)
        monkeypatch.setattr(bootstrap.platform, "machine", lambda: "aarch64")
        keys: list[tuple[str, str]] = []
        digest = hashlib.sha256(payload).hexdigest()
        monkeypatch.setattr(
            bootstrap, "tool_sha256", lambda n, k: keys.append((n, k)) or digest
        )

        assert bootstrap.install_rustup("stable") == 0

        assert "/aarch64-unknown-linux-gnu/rustup-init" in self._urls(seen)[0]
        assert keys == [("rustup", "aarch64")]
        assert versions.tool_sha256("rustup", "aarch64")

    def test_rustup_digest_mismatch_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The real pin, against bytes that are not rustup-init.
        seen = self._wire(monkeypatch, tmp_path, b"tampered")

        assert bootstrap.install_rustup("stable") == 1
        assert len(seen["curl"]) == 1
        assert seen["run"] == []

    def test_rustup_unknown_cpu_fetches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen = self._wire(monkeypatch, tmp_path, b"")
        monkeypatch.setattr(bootstrap.platform, "machine", lambda: "riscv64")

        assert bootstrap.install_rustup("stable") == 1
        assert seen["curl"] == seen["run"] == []

    def test_install_rust_goes_through_the_pinned_rustup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        asked: list[str] = []
        monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
        monkeypatch.setattr(bootstrap, "_have", lambda _b: False)
        monkeypatch.setattr(bootstrap, "install_rustup", lambda c: asked.append(c) or 0)
        monkeypatch.setattr(bootstrap, "install_sccache", lambda _bin: 0)
        monkeypatch.setattr(bootstrap, "_run", lambda *_a, **_k: 0)
        monkeypatch.setenv("CARGO_HOME", str(tmp_path))
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

        assert bootstrap.install_rust(bootstrap.RustSpec(channels=["stable"])) == 0
        assert asked == ["stable"]

    # Go

    def test_go_is_the_pinned_runtime(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        payload = b"go tarball"
        seen = self._wire(monkeypatch, tmp_path, payload)
        self._pin(monkeypatch, payload)

        assert bootstrap.install_go() == 0

        version = versions.runtime_version("go")
        tarball = f"go{version}.linux-amd64.tar.gz"
        assert self._urls(seen) == [f"https://go.dev/dl/{tarball}"]
        [(cmd, _env, _stdin)] = seen["run"]
        assert cmd[:3] == ["tar", "-C", str(tmp_path)]
        assert Path(cmd[-1]).name == tarball

    def test_go_digest_mismatch_unpacks_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen = self._wire(monkeypatch, tmp_path, b"tampered")

        assert bootstrap.install_go() == 1
        assert len(seen["curl"]) == 1
        assert seen["run"] == []

    def test_go_arm64_takes_the_arm64_pin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        payload = b"go tarball"
        seen = self._wire(monkeypatch, tmp_path, payload)
        monkeypatch.setattr(bootstrap.platform, "machine", lambda: "arm64")
        keys: list[tuple[str, str]] = []
        digest = hashlib.sha256(payload).hexdigest()
        monkeypatch.setattr(
            bootstrap, "runtime_sha256", lambda n, k: keys.append((n, k)) or digest
        )

        assert bootstrap.install_go() == 0
        assert self._urls(seen)[0].endswith(".linux-arm64.tar.gz")
        assert keys == [("go", "arm64")]
        assert versions.runtime_sha256("go", "arm64")

    def test_go_already_installed_fetches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen = self._wire(monkeypatch, tmp_path, b"")
        (tmp_path / "go" / "bin").mkdir(parents=True)
        (tmp_path / "go" / "bin" / "go").write_bytes(b"")

        assert bootstrap.install_go() == 0
        assert seen["curl"] == []

    # nvm

    def test_nvm_is_the_pinned_tag_and_asks_no_api(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        payload = b"#!/usr/bin/env bash\necho nvm\n"
        seen = self._wire(monkeypatch, tmp_path, payload)
        self._pin(monkeypatch, payload)
        nvm_dir = tmp_path / "nvm"

        assert bootstrap.install_nvm(nvm_dir) == 0

        tag = versions.tool_version("nvm")
        urls = self._urls(seen)
        assert urls == [
            f"https://raw.githubusercontent.com/nvm-sh/nvm/{tag}/install.sh"
        ]
        assert API_HOST not in _hosts(urls)
        [(cmd, env, stdin)] = seen["run"]
        assert cmd[0] == "bash"
        assert Path(cmd[1]).name == "install.sh"
        assert stdin is None
        assert env["NVM_DIR"] == str(nvm_dir)

    def test_nvm_digest_mismatch_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen = self._wire(monkeypatch, tmp_path, b"echo pwned\n")

        assert bootstrap.install_nvm(tmp_path / "nvm") == 1
        assert len(seen["curl"]) == 1
        assert seen["run"] == []

    def test_install_node_asks_no_api_and_refuses_a_bad_script(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The bake shares one anonymous api.github.com quota across the estate."""
        seen = self._wire(monkeypatch, tmp_path, b"echo pwned\n")
        monkeypatch.setenv("NVM_DIR", str(tmp_path / "nvm"))

        assert bootstrap.install_node() == 1
        assert API_HOST not in _hosts(self._urls(seen))
        assert seen["run"] == []

    def test_no_api_github_url_in_the_module(self) -> None:
        source = Path(bootstrap.__file__).read_text(encoding="utf-8")
        assert "api.github.com" not in source


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
        assert f"rustup:      {versions.tool_version('rustup')}" in combined
        assert f"go: {versions.runtime_version('go')}\n" in combined
        assert f"nvm: {versions.tool_version('nvm')}\n" in combined
        assert f"node: {versions.runtime_version('node')}\n" in combined
        assert f"python: {versions.runtime_version('python')}\n" in combined

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
