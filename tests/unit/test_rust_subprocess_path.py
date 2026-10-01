# Project:   HyperI CI
# File:      tests/unit/test_rust_subprocess_path.py
# Purpose:   Strip reporting and the run_cmd path of the Rust handler
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import ast
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.languages.rust import build, pgo, quality
from hyperi_ci.languages.rust.optimize import OptimizationProfile

_RUST_PACKAGE = Path(build.__file__).parent
_NATIVE = "x86_64-unknown-linux-gnu"


class _Reports:
    """Collect what the strip path warns and announces."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, ci: bool) -> None:
        self.warnings: list[str] = []
        self.announced: list[str] = []
        monkeypatch.setattr(build, "is_ci", lambda: ci, raising=False)
        monkeypatch.setattr(build, "warn", self.warnings.append)
        monkeypatch.setattr(
            build, "announce", lambda msg, *_a, **_k: self.announced.append(msg)
        )


def _binary(tmp_path: Path, name: str = "app-linux-amd64") -> Path:
    binary = tmp_path / name
    binary.write_bytes(b"\x7fELF" + b"\0" * 64)
    return binary


class TestMissingStripTool:
    """A binary that cannot be stripped is named, never shipped in silence (#444)."""

    @pytest.mark.parametrize(
        ("target", "tool"),
        [
            (_NATIVE, "strip"),
            ("aarch64-unknown-linux-gnu", "aarch64-linux-gnu-strip"),
        ],
    )
    def test_locally_it_warns_and_names_the_binary(
        self, tmp_path, monkeypatch, target, tool
    ) -> None:
        reports = _Reports(monkeypatch, ci=False)
        monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
        binary = _binary(tmp_path)

        assert build._strip_binary(binary, target) is True

        assert len(reports.warnings) == 1, reports.warnings
        assert binary.name in reports.warnings[0]
        assert "unstripped" in reports.warnings[0]
        assert tool in reports.warnings[0]

    def test_in_ci_it_fails_and_names_the_binary(self, tmp_path, monkeypatch) -> None:
        reports = _Reports(monkeypatch, ci=True)
        monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
        binary = _binary(tmp_path)

        assert build._strip_binary(binary, _NATIVE) is False

        assert len(reports.announced) == 1, reports.announced
        assert binary.name in reports.announced[0]

    def test_in_ci_packaging_stops_before_shipping(self, tmp_path, monkeypatch) -> None:
        _Reports(monkeypatch, ci=True)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
        release = tmp_path / "target" / _NATIVE / "release"
        release.mkdir(parents=True)
        _binary(release, "app")
        monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))

        assert build._package_binaries([_NATIVE], ["app"], "v1.0.0", _NATIVE) == 1

    def test_a_failed_strip_is_reported_too(self, tmp_path, monkeypatch) -> None:
        reports = _Reports(monkeypatch, ci=True)
        tools = tmp_path / "tools"
        tools.mkdir()
        failing = tools / "strip"
        failing.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        failing.chmod(0o755)
        monkeypatch.setenv("PATH", str(tools))
        binary = _binary(tmp_path)

        assert build._strip_binary(binary, _NATIVE) is False
        assert "exited 3" in reports.announced[0]

    def test_a_target_never_stripped_says_nothing(self, tmp_path, monkeypatch) -> None:
        reports = _Reports(monkeypatch, ci=True)
        binary = _binary(tmp_path, "app-windows-amd64.exe")

        assert build._strip_binary(binary, "x86_64-pc-windows-msvc") is True
        assert reports.warnings == reports.announced == []

    @pytest.mark.skipif(
        platform.machine() != "x86_64" or shutil.which("strip") is None,
        reason="needs an x86_64 host with strip",
    )
    def test_a_real_strip_shrinks_the_binary(self, tmp_path, monkeypatch) -> None:
        reports = _Reports(monkeypatch, ci=True)
        source = tmp_path / "hello.c"
        source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        binary = tmp_path / "hello"
        cc = shutil.which("cc")
        if cc is None:
            pytest.skip("needs a C compiler to make an unstripped binary")
        subprocess.run([cc, "-g", "-o", str(binary), str(source)], check=True)
        before = binary.stat().st_size

        assert build._strip_binary(binary, _NATIVE) is True
        assert binary.stat().st_size < before
        assert reports.announced == []


@pytest.mark.skipif(
    platform.machine() != "x86_64"
    or not all(shutil.which(tool) for tool in ("cc", "ar", "file")),
    reason="needs an x86_64 host with cc, ar and file",
)
class TestStaleRlibDetection:
    """The arch check reads a real object out of a real archive."""

    @pytest.fixture
    def rlib(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        source = tmp_path / "probe.c"
        source.write_text("int probe(void) { return 1; }\n", encoding="utf-8")
        obj = tmp_path / "probe.o"
        subprocess.run(["cc", "-c", "-o", str(obj), str(source)], check=True)
        deps = tmp_path / "target" / "deps"
        deps.mkdir(parents=True)
        archive = deps / "libprobe_sys-0123.rlib"
        subprocess.run(["ar", "rcs", str(archive), str(obj)], check=True)
        # A relative path, as the stale-crate scan passes it.
        monkeypatch.chdir(tmp_path)
        return archive.relative_to(tmp_path)

    def test_a_native_object_is_the_right_arch(self, rlib: Path) -> None:
        assert build._rlib_has_wrong_arch(rlib, "x86-64") is False

    def test_a_native_object_is_the_wrong_arch_for_aarch64(self, rlib: Path) -> None:
        assert build._rlib_has_wrong_arch(rlib, "ARM aarch64") is True


def _process_calls(path: Path) -> list[str]:
    """Return each `subprocess.<spawn>(...)` call in ``path`` as file:line."""
    spawners = {"run", "Popen", "call", "check_call", "check_output", "getoutput"}
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in spawners
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            found.append(f"{path.name}:{node.lineno}")
    return found


def test_the_rust_handler_spawns_nothing_by_hand() -> None:
    """Every child process goes through common.run_cmd or stream_cmd (#444)."""
    offenders = [
        hit
        for path in sorted(_RUST_PACKAGE.glob("*.py"))
        for hit in _process_calls(path)
    ]
    assert offenders == []


class _FakePopen:
    """Stands in for subprocess.Popen under subprocess.run, keeping every launch."""

    launches: list[dict[str, Any]] = []

    def __init__(self, args: list[str], **kwargs: Any) -> None:
        self.args = args
        self.returncode = 0
        self.launch = {"args": list(args), **kwargs}
        _FakePopen.launches.append(self.launch)

    def __enter__(self) -> "_FakePopen":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def communicate(self, _input: Any = None, timeout: float | None = None):
        self.launch["communicate_timeout"] = timeout
        return None, None

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every process subprocess.run would start, starting none."""
    _FakePopen.launches = []
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setenv("HYPERCI_TEST_RUNNER_ENV", "kept")
    monkeypatch.setenv("RUSTC_WRAPPER", "sccache")
    return _FakePopen.launches


def _effective_env(launch: dict[str, Any]) -> dict[str, str]:
    env = launch.get("env")
    return dict(os.environ) if env is None else dict(env)


class TestCargoLaunchesKeepTheirShape:
    """The cargo compiles start with the argv, env, cwd and streaming they had.

    Cargo hashes the profile settings into `-C metadata`, so an env that drifts
    between the PGO compiles breaks profile matching.
    """

    _PROJECT_ENV = {
        "CARGO_PROFILE_RELEASE_LTO": "fat",
        "RUST_FEATURES": "",
        "RUST_ALL_FEATURES": "false",
    }

    def test_every_pgo_and_bolt_compile(self, tmp_path, monkeypatch, launches) -> None:
        release = tmp_path / "target" / _NATIVE / "release"
        release.mkdir(parents=True)
        (release / "app").touch()
        (release / "app-bolt-instrumented").touch()
        for name in (
            "_ensure_cargo_pgo_installed",
            "_ensure_ld_lld_available",
            "_ensure_llvm_profdata_available",
            "_ensure_llvm_bolt_available",
            "_install_bolt_output",
        ):
            monkeypatch.setattr(pgo, name, lambda *_a, **_k: True)
        monkeypatch.setattr(pgo, "_run_workload", lambda *_a, **_k: 0)
        monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
        monkeypatch.delenv("HYPERCI_BOLT_OPTIMIZE_ARGS", raising=False)

        rc = pgo.run_pgo_build(
            target=_NATIVE,
            profile=OptimizationProfile(
                channel="release",
                allocator="system",
                pgo_enabled=True,
                pgo_workload_cmd="true",
                bolt_enabled=True,
            ),
            binary_name="app",
            cwd=tmp_path,
            extra_env=dict(self._PROJECT_ENV),
        )

        assert rc == 0
        runner = dict(os.environ)
        shared = {**runner, **self._PROJECT_ENV, "CARGO_PROFILE_RELEASE_STRIP": "none"}
        profile_use = {**shared, "RUSTC_WRAPPER": ""}
        bolt = {
            **profile_use,
            "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS": (
                "-C link-arg=-fuse-ld=lld"
            ),
        }
        expected = [
            (["cargo", "pgo", "build", "--", "--target", _NATIVE], shared),
            (["cargo", "pgo", "optimize", "--", "--target", _NATIVE], profile_use),
            (
                ["cargo", "pgo", "bolt", "build", "--with-pgo", "--"]
                + ["--target", _NATIVE],
                bolt,
            ),
            (
                ["cargo", "pgo", "bolt", "optimize", "--with-pgo", "--"]
                + ["--target", _NATIVE],
                bolt,
            ),
        ]
        assert [(launch["args"], _effective_env(launch)) for launch in launches] == (
            expected
        )
        for launch in launches:
            assert launch.get("cwd") == tmp_path
            assert launch.get("stdout") is None
            assert launch.get("stderr") is None
            assert launch["communicate_timeout"] is None

    def test_the_plain_fallback_build(self, tmp_path, launches) -> None:
        rc = pgo._run_plain_release_build(
            _NATIVE, ["--features", "jemalloc"], tmp_path, dict(self._PROJECT_ENV)
        )

        assert rc == 0
        (launch,) = launches
        assert launch["args"] == [
            "cargo",
            "build",
            "--release",
            "--target",
            _NATIVE,
            "--features",
            "jemalloc",
        ]
        assert _effective_env(launch) == {**os.environ, **self._PROJECT_ENV}
        assert launch.get("cwd") == tmp_path
        assert launch.get("stdout") is None
        assert launch.get("stderr") is None

    def test_the_plain_build_for_a_target(self, monkeypatch, launches) -> None:
        monkeypatch.setattr(build, "_ensure_target_installed", lambda _t: True)
        monkeypatch.setattr(build, "_get_native_target", lambda: _NATIVE)

        rc = build._build_for_target(
            _NATIVE, "", False, extra_env={"CARGO_PROFILE_RELEASE_LTO": "fat"}
        )

        assert rc == 0
        (launch,) = launches
        assert launch["args"] == ["cargo", "build", "--release", "--target", _NATIVE]
        assert _effective_env(launch) == {
            **os.environ,
            "CARGO_PROFILE_RELEASE_LTO": "fat",
        }
        assert launch.get("stdout") is None
        assert launch.get("stderr") is None


def test_the_feature_matrix_reads_one_merged_pipe(monkeypatch, launches) -> None:
    """cargo-hack set names and diagnostics stay in the order cargo wrote them."""
    monkeypatch.setattr(quality.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert quality._run_matrix_pass("clippy", ["cargo", "clippy"], "default", "warn")

    (launch,) = launches
    assert launch["stdout"] is subprocess.PIPE
    assert launch["stderr"] is subprocess.STDOUT
    assert _effective_env(launch) == {**os.environ, "CARGO_TERM_COLOR": "never"}
