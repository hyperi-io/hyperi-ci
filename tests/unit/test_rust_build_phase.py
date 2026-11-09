# Project:   HyperI CI
# File:      tests/unit/test_rust_build_phase.py
# Purpose:   Tests for the release build split into one CI job per phase
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A PGO + BOLT build run as three jobs, each starting from a fresh checkout."""

import os
import shutil
import stat

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import build, build_phase, pgo
from hyperi_ci.languages.rust.build_phase import (
    BOLT,
    OPTIMIZE,
    PHASE_ENV,
    PROFILE,
    PhaseError,
    PhaseState,
)
from hyperi_ci.languages.rust.optimize import OptimizationOutcome, OptimizationProfile

TARGET = "x86_64-unknown-linux-gnu"
MOLD_KEY = "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(pgo.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(pgo, "_BOLT_LINKER_DIR", tmp_path / "bolt-linker")
    monkeypatch.setenv("PATH", os.environ["PATH"])
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    monkeypatch.delenv(PHASE_ENV, raising=False)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        (" pgo-profile ", PROFILE),
        (OPTIMIZE, OPTIMIZE),
        (BOLT, BOLT),
    ],
)
def test_the_phase_comes_from_the_environment(value, expected):
    environ = {} if value is None else {PHASE_ENV: value}
    assert build_phase.build_phase(environ) == expected


@pytest.mark.parametrize("value", ["pgo", "optimise", "bolt-optimize"])
def test_an_unknown_phase_is_refused(value):
    with pytest.raises(PhaseError, match="not a build phase"):
        build_phase.build_phase({PHASE_ENV: value})


def _release(cwd):
    release = cwd / "target" / TARGET / "release"
    release.mkdir(parents=True, exist_ok=True)
    return release


def test_stash_and_restore_carry_profiles_binaries_and_state(tmp_path):
    profiles = tmp_path / "target" / "pgo-profiles"
    profiles.mkdir(parents=True)
    (profiles / "a.profraw").write_bytes(b"counts")
    (_release(tmp_path) / "app").write_bytes(b"pgo binary")
    state = PhaseState(mold=True, bolt=True, pgo_applied=True)
    build_phase.stash(tmp_path, TARGET, state, ["app"])

    # The next job starts from a fresh checkout: only the stash survives.
    shutil.rmtree(tmp_path / "target")
    stashed = build_phase.stash_dir(tmp_path, TARGET) / "bin" / "app"
    stashed.chmod(0o644)  # upload-artifact keeps no mode bits

    assert build_phase.restore(tmp_path, TARGET) == state
    assert (profiles / "a.profraw").read_bytes() == b"counts"
    binary = _release(tmp_path) / "app"
    assert binary.read_bytes() == b"pgo binary"
    assert binary.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize(
    "content",
    [None, "not json", '{"done": true, "flavour": "x"}', "[]"],
    ids=["missing", "garbage", "unknown-key", "wrong-shape"],
)
def test_an_unreadable_state_is_a_phase_error(tmp_path, content):
    root = build_phase.stash_dir(tmp_path, TARGET)
    root.mkdir(parents=True)
    if content is not None:
        (root / "state.json").write_text(content, encoding="utf-8")
    with pytest.raises(PhaseError, match="previous build phase"):
        build_phase.restore(tmp_path, TARGET)


def _profile(*, bolt: bool, setup: str | None = None) -> OptimizationProfile:
    return OptimizationProfile(
        channel="release",
        allocator="system",
        lto="fat",
        pgo_enabled=True,
        pgo_workload_cmd="bash scripts/w.sh",
        pgo_workload_setup_cmd=setup,
        bolt_enabled=bolt,
    )


class _Toolchain:
    """Record what each phase ran, with no real cargo, workload or llvm-bolt."""

    def __init__(self, monkeypatch, cwd, *, bolt_ok=True):
        self.cwd = cwd
        self.cargo: list[tuple[str, dict]] = []
        self.ran: list[str] = []
        monkeypatch.setattr(pgo, "_ensure_cargo_pgo_installed", lambda: True)
        monkeypatch.setattr(pgo, "_ensure_llvm_profdata_available", lambda: True)
        monkeypatch.setattr(pgo, "_ensure_llvm_bolt_available", lambda *_a: True)
        monkeypatch.setattr(pgo, "_direct_bolt_link_env", lambda *_a: {})
        monkeypatch.setattr(pgo, "_run_cargo_pgo", self._cargo)
        monkeypatch.setattr(
            pgo, "_run_workload", lambda *_a, **_k: self.ran.append("workload") or 0
        )
        monkeypatch.setattr(
            pgo, "_run_workload_setup", lambda *_a: self.ran.append("setup") or 0
        )
        monkeypatch.setattr(pgo, "log_peak_memory", lambda _phase: None)

        def direct_bolt(_t, _bins, _wb, _p, _cwd, outcome):
            self.ran.append("bolt")
            if outcome and bolt_ok:
                outcome.bolt_applied = True
            return bolt_ok

        monkeypatch.setattr(pgo, "_direct_bolt", direct_bolt)

    def _cargo(self, args, cwd, extra_env):
        self.cargo.append((args[0], dict(extra_env or {})))
        if args[0] == "build":
            profiles = cwd / "target" / "pgo-profiles"
            profiles.mkdir(parents=True, exist_ok=True)
            (profiles / "app.profraw").write_bytes(b"counts")
        (_release(cwd) / "app").write_bytes(f"{args[0]} output".encode())
        return 0


def _phase(phase, cwd, profile, outcome=None):
    return pgo.run_pgo_phase(
        phase,
        target=TARGET,
        profile=profile,
        binary_name="app",
        cwd=cwd,
        outcome=outcome,
    )


def _fresh_checkout(cwd):
    shutil.rmtree(cwd / "target", ignore_errors=True)


def test_three_jobs_do_what_one_job_does(tmp_path, monkeypatch):
    tools = _Toolchain(monkeypatch, tmp_path)
    profile = _profile(bolt=True, setup="bash scripts/up.sh")

    first = OptimizationOutcome()
    assert _phase(PROFILE, tmp_path, profile, first) == 0
    assert first.deferred and not first.pgo_applied
    assert [c for c, _ in tools.cargo] == ["build"]

    _fresh_checkout(tmp_path)
    second = OptimizationOutcome()
    assert _phase(OPTIMIZE, tmp_path, profile, second) == 0
    assert second.deferred and second.pgo_applied
    assert [c for c, _ in tools.cargo] == ["build", "optimize"]

    _fresh_checkout(tmp_path)
    last = OptimizationOutcome()
    assert _phase(BOLT, tmp_path, profile, last) == 0
    assert not last.deferred
    assert (last.pgo_applied, last.bolt_applied) == (True, True)
    # The optimised binary crossed jobs, and the BOLT job set up its services again.
    assert (_release(tmp_path) / "app").read_bytes() == b"optimize output"
    assert tools.ran == ["setup", "workload", "setup", "bolt"]


def test_without_bolt_the_optimise_job_finishes_the_build(tmp_path, monkeypatch):
    tools = _Toolchain(monkeypatch, tmp_path)
    profile = _profile(bolt=False)
    assert _phase(PROFILE, tmp_path, profile) == 0
    _fresh_checkout(tmp_path)
    outcome = OptimizationOutcome()
    assert _phase(OPTIMIZE, tmp_path, profile, outcome) == 0
    assert outcome.pgo_applied and not outcome.deferred

    # The BOLT job still runs in the workflow, and finds nothing to do.
    _fresh_checkout(tmp_path)
    last = OptimizationOutcome()
    assert _phase(BOLT, tmp_path, profile, last) == 0
    assert last.deferred
    assert [c for c, _ in tools.cargo] == ["build", "optimize"]
    assert "bolt" not in tools.ran


def test_the_mold_rescue_reaches_the_optimise_job(tmp_path, monkeypatch):
    tools = _Toolchain(monkeypatch, tmp_path)
    profile = _profile(bolt=False)
    build_phase.stash(tmp_path, TARGET, PhaseState(mold=True))
    assert _phase(OPTIMIZE, tmp_path, profile) == 0
    (_command, env) = tools.cargo[-1]
    assert env[MOLD_KEY] == "-C link-arg=-fuse-ld=mold"


def test_a_build_finished_earlier_runs_nothing_later(tmp_path, monkeypatch):
    tools = _Toolchain(monkeypatch, tmp_path)
    build_phase.stash(tmp_path, TARGET, PhaseState(done=True))
    for phase in (OPTIMIZE, BOLT):
        outcome = OptimizationOutcome()
        assert _phase(phase, tmp_path, _profile(bolt=True), outcome) == 0
        assert outcome.deferred
    assert tools.cargo == []


def test_a_later_phase_without_a_stash_fails(tmp_path, monkeypatch):
    tools = _Toolchain(monkeypatch, tmp_path)
    assert _phase(OPTIMIZE, tmp_path, _profile(bolt=True)) == 1
    assert tools.cargo == []


def test_no_cargo_pgo_builds_plain_and_finishes_in_the_first_job(tmp_path, monkeypatch):
    monkeypatch.setattr(pgo, "_ensure_cargo_pgo_installed", lambda: False)
    plain: list[str] = []
    monkeypatch.setattr(
        pgo, "_run_plain_release_build", lambda *_a: plain.append("plain") or 0
    )
    outcome = OptimizationOutcome()
    assert _phase(PROFILE, tmp_path, _profile(bolt=True), outcome) == 0
    assert plain == ["plain"] and not outcome.deferred
    assert build_phase.read_state(tmp_path, TARGET).done


class TestBuildStage:
    """build.run() for a phase job: skip, refuse, or mark the build done."""

    @staticmethod
    def _run(monkeypatch, tmp_path, phase, *, binaries=("app",)):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(PHASE_ENV, phase)
        monkeypatch.setattr(build.shutil, "which", lambda _cmd: "/usr/bin/cargo")
        monkeypatch.setattr(build, "is_ci", lambda: False)
        monkeypatch.setattr(build, "_get_native_target", lambda: TARGET)
        monkeypatch.setattr(build, "_detect_binary_names", lambda: list(binaries))
        monkeypatch.setattr(build, "_detect_cargo_features", lambda: set())
        monkeypatch.setattr(build, "_resolve_build_channel", lambda _cfg: "release")
        monkeypatch.setattr(build, "_detect_version", lambda: "v1.0.0")
        packaged: list[list[str]] = []
        monkeypatch.setattr(
            build,
            "_package_binaries",
            lambda targets, *_a: packaged.append(targets) or 0,
        )
        monkeypatch.setattr(build, "_verify_bolt_shipped", lambda *_a: 0)
        built: list[str] = []
        monkeypatch.setattr(
            build,
            "_build_for_target",
            lambda target, *_a, **_k: built.append(target) or 0,
        )
        config = CIConfig(language="rust", _raw={})
        return build.run(config, {"RUST_BUILD_TARGETS": TARGET}), built, packaged

    def test_a_later_job_after_a_finished_build_does_nothing(
        self, monkeypatch, tmp_path
    ):
        build_phase.stash(tmp_path, TARGET, PhaseState(done=True))
        rc, built, packaged = self._run(monkeypatch, tmp_path, OPTIMIZE)
        assert (rc, built, packaged) == (0, [], [])

    def test_an_unknown_phase_fails_the_build(self, monkeypatch, tmp_path):
        rc, built, _ = self._run(monkeypatch, tmp_path, "optimise")
        assert (rc, built) == (1, [])

    def test_a_library_profile_job_finishes_the_build(self, monkeypatch, tmp_path):
        rc, built, _ = self._run(monkeypatch, tmp_path, PROFILE, binaries=())
        assert (rc, built) == (0, [TARGET])
        assert build_phase.read_state(tmp_path, TARGET).done
