# Project:   HyperI CI
# File:      tests/unit/test_rust_cpu_floor.py
# Purpose:   Tests for the x86_64 CPU floor on Rust target rustflags
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.languages.rust.cpu_floor` and its callers."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperi_ci import dispatch
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import build, cpu_floor, pgo
from hyperi_ci.languages.rust.cpu_floor import (
    config_target_cpu,
    floor_for,
    suspended,
    target_cpu,
    target_rustflags_key,
    with_floor,
)

GNU = "x86_64-unknown-linux-gnu"
MUSL = "x86_64-unknown-linux-musl"
GNU_KEY = target_rustflags_key(GNU)
FLOOR = "-C target-cpu=x86-64-v3 -C target-feature=+pclmulqdq"
MOLD = "-C link-arg=-fuse-ld=mold"


def _config(**floor: Any) -> CIConfig:
    return CIConfig(_raw={"build": {"rust": {"cpu_floor": {"enabled": True, **floor}}}})


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo dir with an empty CARGO_HOME beside it."""
    (tmp_path / "home").mkdir()
    (tmp_path / "repo").mkdir()
    return tmp_path / "repo"


def _environ(repo: Path, **extra: str) -> dict[str, str]:
    return {"CARGO_HOME": str(repo.parent / "home"), **extra}


@pytest.mark.parametrize(
    ("flags", "cpu"),
    [
        (["-C", "target-cpu=x86-64-v3"], "x86-64-v3"),
        (["-Ctarget-cpu=native"], "native"),
        (["-C", "target-cpu=x86-64", "-C", "target-cpu=x86-64-v4"], "x86-64-v4"),
        (["-C", "target-feature=+crt-static"], None),
        ([], None),
        (["-C"], None),
    ],
)
def test_target_cpu_takes_the_last_one(flags: list[str], cpu: str | None) -> None:
    assert target_cpu(flags) == cpu


@pytest.mark.parametrize(
    ("config_toml", "found"),
    [
        (None, None),
        (f'[target.{GNU}]\nrustflags = ["-C", "target-cpu=native"]\n', "native"),
        (f'[target.{GNU}]\nrustflags = "-C target-cpu=x86-64-v3"\n', "x86-64-v3"),
        ('[target.\'cfg(unix)\']\nrustflags = ["-C", "target-cpu=x86-64"]\n', "x86-64"),
        (f'[target.{MUSL}]\nrustflags = ["-C", "target-cpu=native"]\n', None),
        ('[build]\nrustflags = ["-C", "target-cpu=native"]\n', None),
        (f'[target.{GNU}]\nrustflags = ["-C", "link-arg=-fuse-ld=mold"]\n', None),
        ("not = [toml", None),
    ],
)
def test_config_target_cpu(
    repo: Path, config_toml: str | None, found: str | None
) -> None:
    if config_toml is not None:
        _write(repo / ".cargo" / "config.toml", config_toml)
    assert config_target_cpu(GNU, repo, repo.parent / "home") == found


def test_config_target_cpu_reads_cargo_home(repo: Path) -> None:
    _write(
        repo.parent / "home" / "config.toml",
        f'[target.{GNU}]\nrustflags = ["-C", "target-cpu=native"]\n',
    )
    assert config_target_cpu(GNU, repo, repo.parent / "home") == "native"


@pytest.mark.parametrize(
    ("config", "extra_env", "repo_cpu", "expected"),
    [
        (CIConfig(_raw={}), {}, None, {}),
        (_config(enabled=False), {}, None, {}),
        (_config(), {}, None, {GNU: FLOOR, MUSL: FLOOR}),
        (_config(), {"RUSTFLAGS": "-C opt-level=3"}, None, {}),
        (_config(), {"CARGO_ENCODED_RUSTFLAGS": ""}, None, {}),
        (_config(), {}, "x86-64-v3", {GNU: FLOOR, MUSL: FLOOR}),
        (_config(), {}, "native", {MUSL: FLOOR}),
        (_config(targets=[GNU]), {}, None, {GNU: FLOOR}),
        (
            _config(rustflags=["-C", "target-feature=+avx2"]),
            {},
            "native",
            {
                GNU: "-C target-feature=+avx2",
                MUSL: "-C target-feature=+avx2",
            },
        ),
        (_config(rustflags="x86-64-v3"), {}, None, {GNU: FLOOR, MUSL: FLOOR}),
        (_config(rustflags=[]), {}, None, {GNU: FLOOR, MUSL: FLOOR}),
        (_config(targets="gnu"), {}, None, {GNU: FLOOR, MUSL: FLOOR}),
    ],
    ids=[
        "shipped-off",
        "off",
        "on",
        "rustflags-env",
        "encoded-rustflags-env",
        "repo-sets-the-same-cpu",
        "repo-sets-another-cpu",
        "one-target",
        "floor-without-a-cpu",
        "rustflags-not-a-list",
        "rustflags-empty",
        "targets-not-a-list",
    ],
)
def test_floor_for(
    repo: Path,
    config: CIConfig,
    extra_env: dict[str, str],
    repo_cpu: str | None,
    expected: dict[str, str],
) -> None:
    if repo_cpu:
        _write(
            repo / ".cargo" / "config.toml",
            f'[target.{GNU}]\nrustflags = ["-C", "target-cpu={repo_cpu}"]\n',
        )
    assert floor_for(config, _environ(repo, **extra_env), repo) == expected


@pytest.mark.parametrize("runner", [None, MOLD], ids=["no-runner-value", "arc-mold"])
def test_stage_adds_the_floor_and_puts_the_runner_value_back(
    repo: Path, runner: str | None
) -> None:
    environ = _environ(repo)
    if runner:
        environ[GNU_KEY] = runner

    with cpu_floor.cpu_floor(_config(targets=[GNU]), environ, cwd=repo):
        assert environ[GNU_KEY] == " ".join(p for p in (runner, FLOOR) if p)
        assert (
            with_floor(GNU, "-C link-arg=-fuse-ld=lld")
            == f"-C link-arg=-fuse-ld=lld {FLOOR}"
        )
        with suspended(environ):
            assert environ.get(GNU_KEY) == runner
        assert environ[GNU_KEY].endswith(FLOOR)

    assert environ.get(GNU_KEY) == runner
    assert with_floor(GNU, MOLD) == MOLD


def test_stage_restores_on_error(repo: Path) -> None:
    environ = _environ(repo, **{GNU_KEY: MOLD})
    with pytest.raises(RuntimeError), cpu_floor.cpu_floor(_config(), environ, cwd=repo):
        raise RuntimeError
    assert environ[GNU_KEY] == MOLD
    assert with_floor(GNU, "") == ""


def test_floor_off_changes_nothing(repo: Path) -> None:
    environ = _environ(repo, **{GNU_KEY: MOLD})
    with cpu_floor.cpu_floor(CIConfig(_raw={}), environ, cwd=repo):
        assert environ[GNU_KEY] == MOLD
        with suspended(environ):
            assert environ[GNU_KEY] == MOLD


@pytest.mark.parametrize(
    ("target", "linker_flags"),
    [
        (GNU, "-C link-arg=-fuse-ld=lld"),
        ("aarch64-unknown-linux-gnu", "-C link-arg=-fuse-ld=lld"),
    ],
)
def test_bolt_link_steps_keep_the_floor(
    repo: Path, monkeypatch: pytest.MonkeyPatch, target: str, linker_flags: str
) -> None:
    monkeypatch.setattr(pgo, "_a53_linker_env", lambda *_a: {})
    key = target_rustflags_key(target)
    with cpu_floor.cpu_floor(_config(targets=[GNU]), _environ(repo), cwd=repo):
        bolt = pgo._bolt_build_env(target)[key]
        direct = pgo._direct_bolt_link_env(target, None)[key]
    floor = f" {FLOOR}" if target == GNU else ""
    assert bolt == f"{linker_flags}{floor}"
    assert direct == f"{linker_flags}{floor} -C link-arg=-Wl,--emit-relocs"


def test_mold_relink_keeps_the_floor(repo: Path) -> None:
    pipeline = SimpleNamespace(target=GNU, build_env=None, mold=False)
    with cpu_floor.cpu_floor(_config(), _environ(repo), cwd=repo):
        pgo._Pipeline.use_mold(pipeline)  # type: ignore[arg-type]
    assert pipeline.build_env == {GNU_KEY: f"{MOLD} {FLOOR}"}


def test_dispatch_runs_a_rust_stage_on_the_floor(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CARGO_HOME", str(repo.parent / "home"))
    monkeypatch.setenv("CARGO_BUILD_JOBS", "2")
    monkeypatch.setenv(GNU_KEY, MOLD)
    for name in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        monkeypatch.delenv(name, raising=False)
    seen: dict[str, str | None] = {}

    def run(_config: CIConfig, *, extra_env: dict[str, str] | None = None) -> int:
        seen["inside"] = os.environ.get(GNU_KEY)
        return 0

    handler = SimpleNamespace(run=run)
    monkeypatch.setattr(dispatch, "_find_handler_module", lambda *_a: handler)

    assert dispatch._dispatch_to_handler("rust", "test", _config()) == 0
    assert seen["inside"] == f"{MOLD} {FLOOR}"
    assert os.environ[GNU_KEY] == MOLD


def test_wheel_builds_run_without_the_floor(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyperi_ci.languages.rust import bindings

    monkeypatch.setenv(GNU_KEY, MOLD)
    seen: list[str | None] = []

    def run_cmd(cmd: list[str], **_k: Any) -> SimpleNamespace:
        seen.append(os.environ.get(GNU_KEY))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bindings, "run_cmd", run_cmd)
    binding = bindings.Binding(path=repo, requires_python="")
    with cpu_floor.cpu_floor(_config(), cwd=repo):
        rc = bindings._build_one(
            binding, bindings.WheelSpec(GNU), repo / "out", repo, host=GNU
        )
        assert os.environ[GNU_KEY] == f"{MOLD} {FLOOR}"
    assert rc == 0
    assert seen == [MOLD]


# Stands in for qemu-x86_64: plan.json beside it says how each -cpu run ends.
_FAKE_QEMU = """#!{python}
import json, os, signal, sys, time
from pathlib import Path
plan = json.loads((Path(sys.argv[0]).parent / "plan.json").read_text())[sys.argv[2]]
if plan == "sigill":
    os.kill(os.getpid(), signal.SIGILL)
if plan == "hang":
    time.sleep(30)
sys.stderr.write(plan[1])
sys.exit(plan[0])
"""

REFUSES = [
    1,
    "error: this CPU reports no CPUID leaf 7.\n"
    "This build needs an x86-64-v3 CPU with PCLMULQDQ: Intel Haswell (2013)...\n",
]
RUNS = [0, ""]


@pytest.fixture
def fake_qemu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Put a fake qemu-x86_64 on PATH. Call the result with each CPU's plan."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    qemu = bin_dir / "qemu-x86_64"
    qemu.write_text(_FAKE_QEMU.format(python=sys.executable), encoding="utf-8")
    qemu.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cpu_floor, "_QEMU_TIMEOUT", 2)

    def plan(below: object, at: object) -> Path:
        (bin_dir / "plan.json").write_text(
            json.dumps({"Westmere": below, "Haswell": at}), encoding="utf-8"
        )
        return qemu

    return plan


@pytest.mark.parametrize(
    ("below", "at", "failing"),
    [
        (REFUSES, RUNS, []),
        (REFUSES, [2, "error: unexpected argument '--version'\n"], []),
        (REFUSES, "hang", []),
        ("sigill", RUNS, ["Westmere: killed by SIGILL"]),
        (RUNS, RUNS, ["Westmere: exit 0"]),
        ([1, "config not found\n"], RUNS, ["Westmere: exit 1 (config not found)"]),
        ("hang", RUNS, ["Westmere: still running after 2s"]),
        (REFUSES, "sigill", ["Haswell: killed by SIGILL"]),
        (REFUSES, REFUSES, ["Haswell: exit 1 (error: this CPU reports"]),
    ],
    ids=[
        "refuses-then-runs",
        "floor-cpu-exits-nonzero",
        "floor-cpu-keeps-running",
        "no-check-sigill",
        "no-check-runs",
        "exit-1-without-the-message",
        "below-floor-hangs",
        "floor-cpu-sigill",
        "floor-cpu-refused",
    ],
)
def test_floor_problems(
    fake_qemu: Any, tmp_path: Path, below: object, at: object, failing: list[str]
) -> None:
    qemu = fake_qemu(below, at)
    problems = cpu_floor.floor_problems(str(qemu), tmp_path / "app-linux-amd64")
    assert len(problems) == len(failing)
    for problem, part in zip(problems, failing, strict=True):
        assert f"app-linux-amd64 on -cpu {part}" in problem
    if failing and failing[0].startswith("Westmere"):
        assert "scalo::require_cpu_floor!()" in problems[0]


@pytest.mark.parametrize(
    ("mode", "below", "passes"),
    [
        ("blocking", REFUSES, True),
        ("blocking", "sigill", False),
        ("warn", "sigill", True),
        ("disabled", "sigill", True),
    ],
)
def test_check_binary_modes(
    fake_qemu: Any, tmp_path: Path, mode: str, below: object, passes: bool
) -> None:
    fake_qemu(below, RUNS)
    assert cpu_floor.check_binary(tmp_path / "app", mode) is passes


@pytest.mark.parametrize(
    ("ci", "installs", "passes"),
    [(False, False, True), (True, False, False)],
    ids=["local-skips", "ci-install-fails"],
)
def test_check_binary_without_qemu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ci: bool,
    installs: bool,
    passes: bool,
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(cpu_floor, "is_ci", lambda: ci)
    monkeypatch.setattr(cpu_floor, "apt_install", lambda _p: 0 if installs else 100)
    assert cpu_floor.check_binary(tmp_path / "app", "blocking") is passes


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        (None, "blocking"),
        ("warn", "warn"),
        ("disabled", "disabled"),
        ("off", "blocking"),
    ],
)
def test_check_mode(value: str | None, mode: str) -> None:
    raw = {} if value is None else {"check": value}
    assert cpu_floor.check_mode(_config(**raw)) == mode


@pytest.mark.parametrize(
    ("floored", "mode", "rc"),
    [(True, "blocking", 1), (True, "warn", 0), (False, "blocking", 0)],
    ids=["floor-blocks", "floor-warns", "no-floor-no-check"],
)
def test_packaging_checks_floor_binaries(
    fake_qemu: Any,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    floored: bool,
    mode: str,
    rc: int,
) -> None:
    fake_qemu("sigill", RUNS)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    built = repo / "target" / GNU / "release" / "app"
    _write(built, "binary")
    monkeypatch.setattr(build, "_strip_binary", lambda *_a: True)
    monkeypatch.setattr(build, "_verify_binary", lambda *_a: True)
    config = _config(targets=[GNU]) if floored else CIConfig(_raw={})
    with cpu_floor.cpu_floor(config, _environ(repo), cwd=repo):
        assert build._package_binaries([GNU], ["app"], floor_check=mode) == rc
