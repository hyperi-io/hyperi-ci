# Project:   HyperI CI
# File:      tests/unit/test_native_tools.py
# Purpose:   Tests for the pinned release-binary installer (no real downloads)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import errno
import hashlib
import io
import os
import re
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import IO

import pytest

from hyperi_ci import dispatch, native_tools
from hyperi_ci.common import run_cmd
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.versions import tool_sha256, tool_version


def _config(tools: object = None) -> CIConfig:
    return CIConfig(_raw={} if tools is None else {"test": {"native_tools": tools}})


def _targz(
    *members: str,
    data: bytes = b"#!/bin/sh\necho v0\n",
    symlinks: tuple[str, ...] = (),
    dirs: tuple[str, ...] = (),
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name in dirs:
            folder = tarfile.TarInfo(name=name)
            folder.type = tarfile.DIRTYPE
            archive.addfile(folder)
        for name in symlinks:
            link = tarfile.TarInfo(name=name)
            link.type = tarfile.SYMTYPE
            link.linkname = "elsewhere"
            archive.addfile(link)
        for name in members:
            entry = tarfile.TarInfo(name=name)
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

    def _download(name: str, url: str, dest: Path, **_kw: object) -> bool:
        state["urls"].append(url)
        dest.write_bytes(state["payload"])
        return True

    monkeypatch.setattr(native_tools, "download_to_file", _download)
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
        assert "Known: clickhouse, helm, kustomize, tofu" in str(exc.value)

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
            ("clickhouse", "amd64", "/{v}/clickhouse-common-static-{n}-amd64.tgz"),
            ("clickhouse", "arm64", "/{v}/clickhouse-common-static-{n}-arm64.tgz"),
        ],
    )
    def test_asset_spelling(self, name: str, arch: str, tail: str) -> None:
        version = tool_version(name)
        bare = version.removeprefix("v")
        numbers = re.match(r"\d+(?:\.\d+)*", bare)
        assert numbers
        url, _ = native_tools._asset_url(name, arch)
        assert url.endswith(tail.format(v=version, bare=bare, n=numbers[0]))

    @pytest.mark.parametrize("name", sorted(native_tools._TOOLS))
    def test_the_archive_flag_matches_the_asset(self, name: str) -> None:
        url, _ = native_tools._asset_url(name, "amd64")
        assert native_tools._TOOLS[name].archive == url.endswith((".tar.gz", ".tgz"))


@pytest.mark.usefixtures("linux_amd64")
class TestInstall:
    def test_installs_the_pinned_asset_into_the_cache(
        self, tmp_path: Path, served: dict
    ) -> None:
        bin_dir = native_tools.install_tool("helm", tmp_path)
        version = tool_version("helm")
        digest = hashlib.sha256(served["payload"]).hexdigest()
        assert served["urls"] == [
            f"https://get.helm.sh/helm-{version}-linux-amd64.tar.gz"
        ]
        assert bin_dir == (
            tmp_path / "native-tools" / "helm" / f"{version}-amd64-{digest[:12]}"
        )
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

    def test_a_reissued_pin_never_reuses_the_old_copy(
        self, tmp_path: Path, served: dict
    ) -> None:
        first = native_tools.install_tool("helm", tmp_path)
        served["payload"] = _targz("linux-amd64/helm", data=b"#!/bin/sh\necho v1\n")
        second = native_tools.install_tool("helm", tmp_path)
        assert first is not None
        assert second is not None
        assert first != second
        assert len(served["urls"]) == 2
        assert (second / "helm").read_bytes() == b"#!/bin/sh\necho v1\n"

    def test_a_cached_raw_binary_is_rehashed_not_refetched(
        self, tmp_path: Path, served: dict
    ) -> None:
        served["payload"] = b"\x7fELF-hadolint"
        first = native_tools.install_tool("hadolint", tmp_path)
        second = native_tools.install_tool("hadolint", tmp_path)
        assert first == second
        assert len(served["urls"]) == 1

    def test_a_tampered_raw_binary_is_reinstalled(
        self, tmp_path: Path, served: dict
    ) -> None:
        served["payload"] = b"\x7fELF-hadolint"
        bin_dir = native_tools.install_tool("hadolint", tmp_path)
        assert bin_dir is not None
        (bin_dir / "hadolint").write_bytes(b"swapped")
        assert native_tools.install_tool("hadolint", tmp_path) == bin_dir
        assert len(served["urls"]) == 2
        assert (bin_dir / "hadolint").read_bytes() == served["payload"]

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

    def test_a_raw_digest_mismatch_installs_nothing(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        served["payload"] = b"\x7fELF-hadolint"
        monkeypatch.setattr(native_tools, "tool_sha256", lambda n, a: "0" * 64)
        assert native_tools.install_tool("hadolint", tmp_path) is None
        assert served["urls"][0].endswith("/hadolint-linux-x86_64")
        assert not (tmp_path / "native-tools").exists()
        assert native_tools.install_into("hadolint", tmp_path / "bin") is None
        assert not (tmp_path / "bin").exists()

    def test_an_archive_without_the_binary_installs_nothing(
        self, tmp_path: Path, served: dict
    ) -> None:
        served["payload"] = _targz("linux-amd64/README.md")
        assert native_tools.install_tool("helm", tmp_path) is None
        assert not (tmp_path / "native-tools").exists()

    def test_two_files_with_the_tools_name_install_nothing(
        self, tmp_path: Path, served: dict
    ) -> None:
        served["payload"] = _targz("linux-amd64/helm", "extras/helm")
        assert native_tools.install_tool("helm", tmp_path) is None
        assert not (tmp_path / "native-tools").exists()

    def test_a_symlink_or_directory_named_for_the_tool_is_not_picked(
        self, tmp_path: Path, served: dict
    ) -> None:
        served["payload"] = _targz(
            "linux-amd64/helm", symlinks=("helm",), dirs=("docs/helm",)
        )
        binary = native_tools.install_into("helm", tmp_path)
        assert binary == tmp_path / "helm"
        assert binary.read_bytes().startswith(b"#!/bin/sh")

    def test_a_symlink_alone_is_not_a_binary(
        self, tmp_path: Path, served: dict
    ) -> None:
        served["payload"] = _targz(symlinks=("helm",), dirs=("helm-docs/helm",))
        assert native_tools.install_into("helm", tmp_path / "bin") is None
        assert not (tmp_path / "bin").exists()

    def test_a_failed_rename_leaves_no_temporary_file(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a: object) -> None:
            raise OSError("rename refused")

        monkeypatch.setattr(native_tools.os, "replace", _fail)
        assert native_tools.install_into("helm", tmp_path) is None
        assert list(tmp_path.iterdir()) == []

    def test_a_failed_download_installs_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            native_tools, "download_to_file", lambda n, u, d, **k: False
        )
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


_MULTI_CALL = b'#!/bin/sh\necho "$(basename "$0") $*"\n'


def _clickhouse_tgz(data: bytes = _MULTI_CALL) -> bytes:
    """Lay a tarball out like clickhouse-common-static.

    Besides the binary it holds an alias link and a bash completion file that is
    also named ``clickhouse``.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        alias = tarfile.TarInfo(name="pkg-1/usr/bin/clickhouse-extract-from-config")
        alias.type = tarfile.SYMTYPE
        alias.linkname = "clickhouse"
        archive.addfile(alias)
        for path, body in (
            ("pkg-1/usr/share/bash-completion/completions/clickhouse", b"complete"),
            ("pkg-1/usr/bin/clickhouse", data),
        ):
            entry = tarfile.TarInfo(name=path)
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
    return buf.getvalue()


def _digest_mismatch(mp: pytest.MonkeyPatch, served: dict) -> None:
    mp.setattr(native_tools, "tool_sha256", lambda n, a: "0" * 64)


def _truncated_archive(mp: pytest.MonkeyPatch, served: dict) -> None:
    served["payload"] = served["payload"][:-30]


def _write_interrupted(mp: pytest.MonkeyPatch, served: dict) -> None:
    def _half(source: object, out: io.BufferedWriter, length: int = 0) -> None:
        out.write(b"half a binary")
        raise OSError(errno.ENOSPC, "No space left on device")

    mp.setattr(native_tools.shutil, "copyfileobj", _half)


def _symlink_refused(mp: pytest.MonkeyPatch, served: dict) -> None:
    def _refuse(*_a: object, **_k: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    mp.setattr(native_tools.os, "symlink", _refuse)


@pytest.mark.usefixtures("linux_amd64")
class TestMultiCallBinaryFromALargeArchive:
    """clickhouse: a 265 MB tarball holding a 890 MB binary and a link to it."""

    @pytest.fixture(autouse=True)
    def _clickhouse(self, served: dict) -> None:
        served["payload"] = _clickhouse_tgz()

    @pytest.fixture
    def scratch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point the download scratch at a directory the test can inspect."""
        folder = tmp_path / "scratch"
        folder.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(folder))
        return folder

    def test_installs_the_binary_and_a_link_to_it(
        self, tmp_path: Path, served: dict
    ) -> None:
        binary = native_tools.install_into("clickhouse", tmp_path)
        link = tmp_path / "clickhouse-local"
        assert binary == tmp_path / "clickhouse"
        assert binary.read_bytes() == _MULTI_CALL
        assert binary.stat().st_mode & stat.S_IXUSR
        assert link.is_symlink()
        assert os.readlink(link) == "clickhouse"
        assert link.resolve() == binary.resolve()
        assert served["urls"][0].endswith("-amd64.tgz")
        # The archive's own alias is not copied: only the binary and our links.
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "clickhouse",
            "clickhouse-local",
        ]

    def test_the_link_dispatches_on_its_own_name(self, tmp_path: Path) -> None:
        native_tools.install_into("clickhouse", tmp_path)
        via_link = run_cmd([str(tmp_path / "clickhouse-local"), "x"], capture=True)
        via_binary = run_cmd([str(tmp_path / "clickhouse"), "x"], capture=True)
        assert via_link.stdout.strip() == "clickhouse-local x"
        assert via_binary.stdout.strip() == "clickhouse x"

    def test_a_file_of_the_same_name_elsewhere_is_not_the_binary(
        self, tmp_path: Path, served: dict
    ) -> None:
        served["payload"] = _targz("pkg-1/usr/share/bash-completion/clickhouse")
        assert native_tools.install_into("clickhouse", tmp_path / "bin") is None
        assert not (tmp_path / "bin").exists()

    def test_the_arm64_asset_is_fetched_on_aarch64(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_tools.platform, "machine", lambda: "aarch64")
        assert native_tools.install_into("clickhouse", tmp_path) is not None
        assert served["urls"][0].endswith("-arm64.tgz")

    def test_the_binary_is_copied_in_bounded_chunks(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[int] = []
        real = shutil.copyfileobj

        def _spy(source: IO[bytes], out: IO[bytes], length: int = 0) -> None:
            seen.append(length)
            real(source, out, length)

        data = bytes(range(256)) * 16
        served["payload"] = _clickhouse_tgz(data)
        monkeypatch.setattr(native_tools, "_CHUNK", 64)
        monkeypatch.setattr(native_tools.shutil, "copyfileobj", _spy)
        binary = native_tools.install_into("clickhouse", tmp_path)
        assert seen == [64]
        assert binary is not None
        assert binary.read_bytes() == data

    def test_the_download_is_hashed_in_chunks(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _whole(*_a: object, **_k: object) -> bytes:
            raise AssertionError("a file was read whole")

        monkeypatch.setattr(Path, "read_bytes", _whole)
        assert native_tools.install_into("clickhouse", tmp_path) is not None

    def test_the_scratch_download_is_removed_after_an_install(
        self, tmp_path: Path, scratch: Path
    ) -> None:
        assert native_tools.install_into("clickhouse", tmp_path / "bin") is not None
        assert list(scratch.iterdir()) == []

    @pytest.mark.parametrize(
        "breakage",
        [_digest_mismatch, _truncated_archive, _write_interrupted, _symlink_refused],
        ids=["hash-mismatch", "truncated-archive", "interrupted-write", "link-refused"],
    )
    def test_a_failure_installs_nothing_and_leaves_no_scratch(
        self,
        tmp_path: Path,
        served: dict,
        scratch: Path,
        monkeypatch: pytest.MonkeyPatch,
        breakage: Callable[[pytest.MonkeyPatch, dict], None],
    ) -> None:
        breakage(monkeypatch, served)
        bin_dir = tmp_path / "bin"
        assert native_tools.install_into("clickhouse", bin_dir) is None
        assert list(scratch.iterdir()) == []
        assert not bin_dir.exists() or list(bin_dir.iterdir()) == []

    def test_an_interrupted_write_keeps_the_copy_already_installed(
        self, tmp_path: Path, served: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "clickhouse").write_bytes(b"old")
        _write_interrupted(monkeypatch, served)
        assert native_tools.install_into("clickhouse", tmp_path) is None
        assert [p.name for p in tmp_path.iterdir()] == ["clickhouse"]
        assert (tmp_path / "clickhouse").read_bytes() == b"old"

    def test_a_cached_install_is_not_fetched_again(
        self, tmp_path: Path, served: dict
    ) -> None:
        first = native_tools.install_tool("clickhouse", tmp_path)
        assert native_tools.install_tool("clickhouse", tmp_path) == first
        assert len(served["urls"]) == 1

    @pytest.mark.parametrize("damage", ["link-gone", "link-dangling"])
    def test_a_cached_install_missing_its_link_is_reinstalled(
        self, tmp_path: Path, served: dict, damage: str
    ) -> None:
        bin_dir = native_tools.install_tool("clickhouse", tmp_path)
        assert bin_dir is not None
        (bin_dir / "clickhouse-local").unlink()
        if damage == "link-dangling":
            (bin_dir / "clickhouse-local").symlink_to("nowhere")
        assert native_tools.install_tool("clickhouse", tmp_path) == bin_dir
        assert len(served["urls"]) == 2
        assert (bin_dir / "clickhouse-local").resolve() == (
            bin_dir / "clickhouse"
        ).resolve()

    def test_the_probe_is_local_version(self) -> None:
        assert native_tools._TOOLS["clickhouse"].probe == ("local", "--version")
        assert "clickhouse" in native_tools.known_tools()


class TestNumericVersion:
    @pytest.mark.parametrize(
        ("version", "numeric"),
        [
            ("v26.10.2.103-stable", "26.10.2.103"),
            ("26.8.1.5-lts", "26.8.1.5"),
            ("v0.40.8", "0.40.8"),
            ("1.2", "1.2"),
            ("nightly", ""),
        ],
    )
    def test_the_dotted_numbers_alone(
        self, monkeypatch: pytest.MonkeyPatch, version: str, numeric: str
    ) -> None:
        tool = native_tools.NativeTool(url="https://x.invalid/{version}/{numeric}")
        monkeypatch.setitem(native_tools._TOOLS, "probe-me", tool)
        monkeypatch.setattr(native_tools, "tool_version", lambda name: version)
        url, _ = native_tools._asset_url("probe-me", "amd64")
        assert url == f"https://x.invalid/{version}/{numeric}"


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
        monkeypatch.setattr(native_tools, "download_to_file", _never)

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

    def test_clickhouse_is_probed_with_local_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "clickhouse"
        script.write_text(
            '#!/bin/sh\n[ "$1 $2" = "local --version" ] || exit 3\n'
            "echo ClickHouse local version 0.0.0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        monkeypatch.setattr(native_tools, "install_tool", lambda name: bin_dir)
        assert native_tools.prepare(_config(["clickhouse"])) == 0

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
