# Project:   HyperI CI
# File:      tests/unit/test_native_tools.py
# Purpose:   Tests for the pinned release-binary installer (no real downloads)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from hyperi_ci import dispatch, native_tools
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.versions import tool_sha256, tool_version


def _config(tools: object = None) -> CIConfig:
    return CIConfig(_raw={} if tools is None else {"test": {"native_tools": tools}})


def _targz(member: str, data: bytes = b"#!/bin/sh\necho v0\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        entry = tarfile.TarInfo(name=member)
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    return buf.getvalue()


def _never(*_a: object, **_k: object) -> None:
    raise AssertionError("nothing may be installed")


@pytest.fixture
def linux_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_tools.sys, "platform", "linux")
    monkeypatch.setattr(native_tools.platform, "machine", lambda: "x86_64")


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Serve one helm tarball through the real digest gate, recording each fetch."""
    state: dict = {"urls": [], "payload": _targz("linux-amd64/helm")}
    monkeypatch.setattr(
        native_tools,
        "tool_sha256",
        lambda name, arch: hashlib.sha256(state["payload"]).hexdigest(),
    )

    def _download(name: str, url: str) -> bytes:
        state["urls"].append(url)
        return state["payload"]

    monkeypatch.setattr(native_tools, "download_artefact", _download)
    return state


class TestTheKey:
    def test_unset_means_nothing(self) -> None:
        assert native_tools.requested_tools(_config()) == []

    def test_empty_list_means_nothing(self) -> None:
        assert native_tools.requested_tools(_config([])) == []

    def test_helm_is_known(self) -> None:
        assert native_tools.requested_tools(_config(["helm"])) == ["helm"]

    def test_a_repeat_installs_once(self) -> None:
        assert native_tools.requested_tools(_config(["helm", "helm"])) == ["helm"]

    def test_unknown_tool_is_refused_by_name(self) -> None:
        with pytest.raises(native_tools.NativeToolError, match="'kubectl'") as exc:
            native_tools.requested_tools(_config(["helm", "kubectl"]))
        assert "Known: helm, kustomize, tofu" in str(exc.value)

    def test_a_gate_tool_is_not_offered_to_the_tests(self) -> None:
        with pytest.raises(native_tools.NativeToolError, match="'gitleaks'"):
            native_tools.requested_tools(_config(["gitleaks"]))

    @pytest.mark.parametrize("raw", ["helm", {"helm": True}, [1], [None]])
    def test_not_a_list_of_names_is_refused(self, raw: object) -> None:
        with pytest.raises(native_tools.NativeToolError, match="list of tool names"):
            native_tools.requested_tools(_config(raw))

    def test_read_from_the_project_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "test:\n  native_tools: [helm]\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        config = load_config(reload=True, project_dir=tmp_path)
        assert native_tools.requested_tools(config) == ["helm"]

    def test_shipped_default_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        config = load_config(reload=True, project_dir=tmp_path)
        assert config.get(native_tools.CONFIG_KEY) == []
        assert native_tools.requested_tools(config) == []


class TestEveryToolIsPinned:
    @pytest.mark.parametrize("name", sorted(native_tools._TOOLS))
    @pytest.mark.parametrize("arch", ["amd64", "arm64"])
    def test_url_and_digest_come_from_the_ssot(self, name: str, arch: str) -> None:
        url, key = native_tools._asset_url(name, arch)
        assert tool_version(name).removeprefix("v") in url
        assert len(tool_sha256(name, key)) == 64

    @pytest.mark.parametrize(
        ("name", "arch", "tail"),
        [
            ("gitleaks", "amd64", "/{v}/gitleaks_{bare}_linux_x64.tar.gz"),
            ("hadolint", "amd64", "/{v}/hadolint-linux-x86_64"),
            ("hadolint", "arm64", "/{v}/hadolint-linux-arm64"),
            ("kube-linter", "amd64", "/{v}/kube-linter-linux"),
            ("kube-linter", "arm64", "/{v}/kube-linter-linux_arm64"),
            ("kubeconform", "arm64", "/{v}/kubeconform-linux-arm64.tar.gz"),
            ("lychee", "amd64", "/lychee-v{v}/lychee-x86_64-unknown-linux-musl.tar.gz"),
            ("alint", "arm64", "/{v}/alint-{v}-aarch64-unknown-linux-musl.tar.gz"),
            ("sccache", "amd64", "/{v}/sccache-{v}-x86_64-unknown-linux-musl.tar.gz"),
        ],
    )
    def test_asset_spelling(self, name: str, arch: str, tail: str) -> None:
        version = tool_version(name)
        url, _ = native_tools._asset_url(name, arch)
        assert url.endswith(tail.format(v=version, bare=version.removeprefix("v")))


@pytest.mark.usefixtures("linux_amd64")
class TestInstall:
    def test_installs_the_pinned_asset_into_the_cache(
        self, tmp_path: Path, served: dict
    ) -> None:
        bin_dir = native_tools.install_tool("helm", tmp_path)
        version = tool_version("helm")
        assert served["urls"] == [
            f"https://get.helm.sh/helm-{version}-linux-amd64.tar.gz"
        ]
        assert bin_dir == tmp_path / "native-tools" / "helm" / f"{version}-amd64"
        binary = bin_dir / "helm"
        assert binary.read_bytes().startswith(b"#!/bin/sh")
        assert binary.stat().st_mode & stat.S_IXUSR
        assert not list(bin_dir.glob(".*.partial"))

    def test_a_cached_install_is_not_fetched_again(
        self, tmp_path: Path, served: dict
    ) -> None:
        first = native_tools.install_tool("helm", tmp_path)
        second = native_tools.install_tool("helm", tmp_path)
        assert first == second
        assert len(served["urls"]) == 1

    def test_arm64_fetches_the_arm64_asset(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools.platform, "machine", lambda: "aarch64")
        served["payload"] = _targz("linux-arm64/helm")
        assert native_tools.install_tool("helm", tmp_path) is not None
        assert served["urls"][0].endswith("-linux-arm64.tar.gz")

    def test_a_digest_mismatch_installs_nothing(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools, "tool_sha256", lambda n, a: "0" * 64)
        assert native_tools.install_tool("helm", tmp_path) is None
        assert not (tmp_path / "native-tools").exists()

    def test_an_archive_without_the_binary_installs_nothing(
        self, tmp_path: Path, served: dict
    ) -> None:
        served["payload"] = _targz("linux-amd64/README.md")
        assert native_tools.install_tool("helm", tmp_path) is None
        assert not (tmp_path / "native-tools").exists()

    def test_a_failed_download_installs_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools, "download_artefact", lambda n, u: None)
        assert native_tools.install_tool("helm", tmp_path) is None
        assert not (tmp_path / "native-tools").exists()

    def test_the_digest_compares_case_insensitively(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        digest = hashlib.sha256(served["payload"]).hexdigest().upper()
        monkeypatch.setattr(native_tools, "tool_sha256", lambda n, a: digest)
        assert native_tools.install_tool("helm", tmp_path) is not None

    def test_a_raw_asset_is_the_binary(self, tmp_path: Path, served: dict) -> None:
        served["payload"] = b"\x7fELF-hadolint"
        binary = native_tools.install_into("hadolint", tmp_path)
        assert binary == tmp_path / "hadolint"
        assert binary.read_bytes() == served["payload"]
        assert served["urls"][0].endswith("/hadolint-linux-x86_64")

    def test_the_binary_is_found_at_any_depth_of_the_archive(
        self, tmp_path: Path, served: dict
    ) -> None:
        served["payload"] = _targz("sccache-v0-x86_64-unknown-linux-musl/sccache")
        assert native_tools.install_into("sccache", tmp_path) == tmp_path / "sccache"

    def test_an_unwritable_directory_is_reported_not_raised(
        self, tmp_path: Path, served: dict
    ) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("", encoding="utf-8")
        assert native_tools.install_into("helm", blocker / "bin") is None

    def test_an_unknown_cpu_fetches_nothing(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools.platform, "machine", lambda: "riscv64")
        assert native_tools.install_into("sccache", tmp_path) is None
        assert served["urls"] == []


@pytest.mark.usefixtures("linux_amd64")
class TestCiBinary:
    def test_an_install_goes_first_on_path(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        monkeypatch.setattr(native_tools.shutil, "which", lambda n: None)
        real_install = native_tools.install_tool
        monkeypatch.setattr(
            native_tools, "install_tool", lambda name: real_install(name, tmp_path)
        )
        exe = native_tools.ci_binary("helm")
        assert exe is not None
        assert os.environ["PATH"].split(os.pathsep)[0] == str(Path(exe).parent)

    def test_a_failed_install_leaves_path_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(native_tools.shutil, "which", lambda n: None)
        monkeypatch.setattr(native_tools, "install_tool", lambda name: None)
        assert native_tools.ci_binary("hadolint") is None
        assert os.environ["PATH"] == "/usr/bin"


class TestOffLinux:
    @pytest.fixture(autouse=True)
    def _darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_tools.sys, "platform", "darwin")
        monkeypatch.setattr(native_tools, "download_artefact", _never)

    def test_uses_a_copy_already_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            native_tools.shutil, "which", lambda n: "/opt/homebrew/bin/helm"
        )
        assert native_tools.install_tool("helm", tmp_path) == Path("/opt/homebrew/bin")

    def test_none_when_there_is_no_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools.shutil, "which", lambda n: None)
        assert native_tools.install_tool("helm", tmp_path) is None


class TestPrepare:
    @staticmethod
    def _fake_helm(bin_dir: Path, exit_code: int = 0) -> Path:
        bin_dir.mkdir(parents=True)
        helm = bin_dir / "helm"
        helm.write_text(
            f"#!/bin/sh\necho v4.3.0+gtest\nexit {exit_code}\n", encoding="utf-8"
        )
        helm.chmod(0o755)
        return bin_dir

    def test_nothing_listed_installs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools, "install_tool", _never)
        assert native_tools.prepare(_config()) == 0

    def test_unknown_tool_fails_before_any_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools, "install_tool", _never)
        assert native_tools.prepare(_config(["nope"])) == 1

    def test_listed_tool_lands_first_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = self._fake_helm(tmp_path / "bin")
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        monkeypatch.setattr(native_tools, "install_tool", lambda name: bin_dir)
        assert native_tools.prepare(_config(["helm"])) == 0
        assert os.environ["PATH"].split(os.pathsep)[0] == str(bin_dir)

    def test_an_install_failure_fails_the_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools, "install_tool", lambda name: None)
        assert native_tools.prepare(_config(["helm"])) == 1

    def test_a_binary_that_does_not_run_fails_the_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = self._fake_helm(tmp_path / "bin", exit_code=3)
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        monkeypatch.setattr(native_tools, "install_tool", lambda name: bin_dir)
        assert native_tools.prepare(_config(["helm"])) == 1


class TestStageTest:
    def test_unknown_tool_fails_without_running_the_tests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dispatch, "_dispatch_to_handler", _never)
        assert dispatch.stage_test("python", _config(["nope"])) == 1

    def test_tools_are_ready_before_the_handler_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        monkeypatch.setattr(
            native_tools, "prepare", lambda config: order.append("tools") or 0
        )
        monkeypatch.setattr(
            dispatch,
            "_dispatch_to_handler",
            lambda *_a, **_k: order.append("tests") or 0,
        )
        assert dispatch.stage_test("rust", _config(["helm"])) == 0
        assert order == ["tools", "tests"]

    def test_by_default_the_tests_run_with_nothing_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools, "install_tool", _never)
        monkeypatch.setattr(dispatch, "_dispatch_to_handler", lambda *_a, **_k: 0)
        assert dispatch.stage_test("python", _config()) == 0
