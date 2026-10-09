# Project:   HyperI CI
# File:      tests/unit/test_rust_subprocess_path.py
# Purpose:   Strip reporting and the run_cmd path of the Rust handler
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import ast
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

        assert build._package_binaries([_NATIVE], ["app"], "v1.0.0") == 1

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

    @pytest.mark.parametrize(
        "target",
        [
            "armv7-unknown-linux-gnueabihf",
            "riscv64gc-unknown-linux-gnu",
            "i686-unknown-linux-gnu",
        ],
    )
    def test_a_linux_target_with_no_known_tool_is_named(
        self, tmp_path, monkeypatch, target
    ) -> None:
        reports = _Reports(monkeypatch, ci=True)
        binary = _binary(tmp_path)

        assert build._strip_binary(binary, target) is False
        assert binary.name in reports.announced[0]
        assert target in reports.announced[0]

    def test_a_target_never_stripped_says_nothing(self, tmp_path, monkeypatch) -> None:
        reports = _Reports(monkeypatch, ci=True)
        binary = _binary(tmp_path, "app.wasm")

        assert build._strip_binary(binary, "wasm32-unknown-unknown") is True
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
    monkeypatch.setenv("CARGO_PROFILE_RELEASE_STRIP", "symbols")
    for name in _WATCHED:
        if name not in _RUNNER_SET:
            monkeypatch.delenv(name, raising=False)
    return _FakePopen.launches


_RUNNER_SET = (
    "HYPERCI_TEST_RUNNER_ENV",
    "RUSTC_WRAPPER",
    "CARGO_PROFILE_RELEASE_STRIP",
)
_RUSTFLAGS = "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS"
_WATCHED = (
    *_RUNNER_SET,
    "CARGO_PROFILE_RELEASE_LTO",
    "RUST_FEATURES",
    "RUST_ALL_FEATURES",
    "CARGO_TERM_COLOR",
    _RUSTFLAGS,
)


def _watched_env(launch: dict[str, Any]) -> dict[str, str | None]:
    """The keys the fixture controls, as the launched process would see them."""
    env = launch["env"]
    assert env is not None, "the launch inherited the env instead of receiving one"
    return {name: env.get(name) for name in _WATCHED}


def _expect(**values: str) -> dict[str, str | None]:
    """The watched keys with the fixture's runner values under ``values``."""
    runner = {
        "HYPERCI_TEST_RUNNER_ENV": "kept",
        "RUSTC_WRAPPER": "sccache",
        "CARGO_PROFILE_RELEASE_STRIP": "symbols",
    }
    return {name: {**runner, **values}.get(name) for name in _WATCHED}


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
            "_ensure_clang_available",
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
        # The runner env is merged in, and the pipeline's profile env beats it.
        shared = _expect(**self._PROJECT_ENV, CARGO_PROFILE_RELEASE_STRIP="none")
        profile_use = {**shared, "RUSTC_WRAPPER": ""}
        bolt = {**profile_use, _RUSTFLAGS: "-C link-arg=-fuse-ld=lld"}
        scope = ["--target", _NATIVE, "--bin", "app"]
        expected = [
            (["cargo", "pgo", "build", "--", *scope], shared),
            (["cargo", "pgo", "optimize", "--", *scope], profile_use),
            (["cargo", "pgo", "bolt", "build", "--with-pgo", "--", *scope], bolt),
            (["cargo", "pgo", "bolt", "optimize", "--with-pgo", "--", *scope], bolt),
        ]
        assert [(launch["args"], _watched_env(launch)) for launch in launches] == (
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
        assert _watched_env(launch) == _expect(**self._PROJECT_ENV)
        assert launch.get("cwd") == tmp_path
        assert launch.get("stdout") is None
        assert launch.get("stderr") is None

    def test_the_plain_build_for_a_target(self, launches) -> None:
        rc = build._build_for_target(
            _NATIVE, "", False, extra_env={"CARGO_PROFILE_RELEASE_LTO": "fat"}
        )

        assert rc == 0
        (launch,) = launches
        assert launch["args"] == ["cargo", "build", "--release", "--target", _NATIVE]
        assert _watched_env(launch) == _expect(CARGO_PROFILE_RELEASE_LTO="fat")
        assert launch.get("stdout") is None
        assert launch.get("stderr") is None


def test_the_feature_matrix_reads_one_merged_pipe(monkeypatch, launches) -> None:
    """cargo-hack set names and diagnostics stay in the order cargo wrote them."""
    monkeypatch.setattr(quality.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert quality._run_matrix_pass("clippy", ["cargo", "clippy"], "default", "warn")

    (launch,) = launches
    assert launch["stdout"] is subprocess.PIPE
    assert launch["stderr"] is subprocess.STDOUT
    assert _watched_env(launch) == _expect(CARGO_TERM_COLOR="never")
