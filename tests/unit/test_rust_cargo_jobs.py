# Project:   HyperI CI
# File:      tests/unit/test_rust_cargo_jobs.py
# Purpose:   Tests for the memory-capped CARGO_BUILD_JOBS every Rust stage runs with
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.languages.rust._jobs` and its wiring in dispatch."""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci import dispatch
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import _jobs
from hyperi_ci.languages.rust._jobs import (
    JOBS_ENV,
    capped_cargo_jobs,
    cargo_jobs_env,
    config_file_setting_jobs,
    jobs_for,
)
from hyperi_ci.memory import GIB, MemoryLimit

QUALITY = "hyperi_ci.languages.rust.quality"

SIXTEEN_GIB = MemoryLimit(16 * GIB, "cgroup v2 /sys/fs/cgroup/memory.max")


def _config(**rust: Any) -> CIConfig:
    return CIConfig(_raw={"build": {"rust": rust}})


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def clean_dir(tmp_path: Path) -> Path:
    """A project dir with no cargo config, and an empty CARGO_HOME beside it."""
    (tmp_path / "home").mkdir()
    (tmp_path / "repo").mkdir()
    return tmp_path


def _environ(root: Path, **extra: str) -> dict[str, str]:
    return {"CARGO_HOME": str(root / "home"), **extra}


class TestJobsFor:
    def test_arc_16cpu_16gi_runner(self) -> None:
        assert jobs_for(16, SIXTEEN_GIB, 2.0) == 8

    def test_cpu_bound_when_memory_is_plentiful(self) -> None:
        assert jobs_for(4, MemoryLimit(64 * GIB, "x"), 2.0) == 4

    def test_floor_of_one_when_memory_is_below_one_job(self) -> None:
        assert jobs_for(16, MemoryLimit(GIB, "x"), 2.0) == 1

    def test_floor_of_one_with_no_cpus(self) -> None:
        assert jobs_for(0, None, 2.0) == 1

    def test_no_limit_leaves_the_cpu_count(self) -> None:
        assert jobs_for(32, None, 2.0) == 32

    def test_fractional_gib_per_job_rounds_down(self) -> None:
        assert jobs_for(16, SIXTEEN_GIB, 1.5) == 10


class TestPrecedence:
    def test_env_is_left_alone(self, clean_dir: Path) -> None:
        env = _environ(clean_dir, **{JOBS_ENV: "3"})
        got = cargo_jobs_env(
            _config(jobs=6),
            env,
            cwd=clean_dir / "repo",
            limit=SIXTEEN_GIB,
            cpus=16,
        )
        assert got == {}

    def test_env_survives_the_stage(self, clean_dir: Path) -> None:
        env = _environ(clean_dir, **{JOBS_ENV: "3"})
        with capped_cargo_jobs(
            _config(), env, cwd=clean_dir / "repo", limit=SIXTEEN_GIB, cpus=16
        ):
            assert env[JOBS_ENV] == "3"
        assert env[JOBS_ENV] == "3"

    def test_build_rust_jobs_wins_over_the_memory_cap(self, clean_dir: Path) -> None:
        got = cargo_jobs_env(
            _config(jobs=12),
            _environ(clean_dir),
            cwd=clean_dir / "repo",
            limit=SIXTEEN_GIB,
            cpus=16,
        )
        assert got == {JOBS_ENV: "12"}

    def test_build_rust_jobs_wins_over_a_cargo_config(self, clean_dir: Path) -> None:
        _write(clean_dir / "repo" / ".cargo" / "config.toml", "[build]\njobs = 4\n")
        got = cargo_jobs_env(
            _config(jobs=2),
            _environ(clean_dir),
            cwd=clean_dir / "repo",
            limit=SIXTEEN_GIB,
            cpus=16,
        )
        assert got == {JOBS_ENV: "2"}

    @pytest.mark.parametrize("value", [True, 0, -2, "lots", 2.5])
    def test_unusable_override_falls_back_to_auto(
        self, clean_dir: Path, value: object
    ) -> None:
        got = cargo_jobs_env(
            _config(jobs=value),
            _environ(clean_dir),
            cwd=clean_dir / "repo",
            limit=SIXTEEN_GIB,
            cpus=16,
        )
        assert got == {JOBS_ENV: "8"}

    def test_auto_caps_by_memory(self, clean_dir: Path) -> None:
        got = cargo_jobs_env(
            _config(jobs="auto", memory_per_job_gib=2),
            _environ(clean_dir),
            cwd=clean_dir / "repo",
            limit=SIXTEEN_GIB,
            cpus=16,
        )
        assert got == {JOBS_ENV: "8"}

    def test_memory_per_job_is_configurable(self, clean_dir: Path) -> None:
        got = cargo_jobs_env(
            _config(memory_per_job_gib=4),
            _environ(clean_dir),
            cwd=clean_dir / "repo",
            limit=SIXTEEN_GIB,
            cpus=16,
        )
        assert got == {JOBS_ENV: "4"}

    @pytest.mark.parametrize("value", [0, -1, "big", False])
    def test_unusable_memory_per_job_uses_the_fallback(
        self, clean_dir: Path, value: object
    ) -> None:
        got = cargo_jobs_env(
            _config(memory_per_job_gib=value),
            _environ(clean_dir),
            cwd=clean_dir / "repo",
            limit=SIXTEEN_GIB,
            cpus=16,
        )
        assert got == {JOBS_ENV: "8"}


class TestCargoConfigJobs:
    """cargo ranks CARGO_BUILD_JOBS above a config file, so ours must not be set."""

    def test_project_config_is_respected(self, clean_dir: Path) -> None:
        _write(clean_dir / "repo" / ".cargo" / "config.toml", "[build]\njobs = 4\n")
        got = cargo_jobs_env(
            _config(),
            _environ(clean_dir),
            cwd=clean_dir / "repo",
            limit=SIXTEEN_GIB,
            cpus=16,
        )
        assert got == {}

    def test_parent_and_legacy_config_name(self, clean_dir: Path) -> None:
        _write(clean_dir / ".cargo" / "config", "[build]\njobs = 4\n")
        nested = clean_dir / "repo" / "crates" / "a"
        nested.mkdir(parents=True)
        found = config_file_setting_jobs(nested, clean_dir / "home")
        assert found == clean_dir / ".cargo" / "config"

    def test_cargo_home_config(self, clean_dir: Path) -> None:
        _write(clean_dir / "home" / "config.toml", "[build]\njobs = 6\n")
        found = config_file_setting_jobs(clean_dir / "repo", clean_dir / "home")
        assert found == clean_dir / "home" / "config.toml"

    def test_config_without_jobs_is_ignored(self, clean_dir: Path) -> None:
        _write(
            clean_dir / "repo" / ".cargo" / "config.toml",
            '[build]\nrustflags = ["-Dwarnings"]\n',
        )
        _write(clean_dir / "home" / "config.toml", "not = [valid toml\n")
        assert config_file_setting_jobs(clean_dir / "repo", clean_dir / "home") is None


class TestCappedCargoJobs:
    def test_sets_then_removes(self, clean_dir: Path) -> None:
        env = _environ(clean_dir)
        with capped_cargo_jobs(
            _config(), env, cwd=clean_dir / "repo", limit=SIXTEEN_GIB, cpus=16
        ):
            assert env[JOBS_ENV] == "8"
        assert JOBS_ENV not in env


class _Recorder:
    """Stands in for the child process, reading the environment it inherits."""

    def __init__(self) -> None:
        self.seen: list[tuple[list[str], str | None]] = []

    def run(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.seen.append((cmd, os.environ.get(JOBS_ENV)))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def matrix_pass(self, _name: str, cmd: list[str], *_a: Any) -> bool:
        self.seen.append((cmd, os.environ.get(JOBS_ENV)))
        return True


def test_the_cap_reaches_a_quality_cargo_call(
    monkeypatch: pytest.MonkeyPatch, clean_dir: Path
) -> None:
    repo = clean_dir / "repo"
    monkeypatch.chdir(repo)
    _write(repo / "deny.toml", "")
    monkeypatch.delenv(JOBS_ENV, raising=False)
    monkeypatch.setenv("CARGO_HOME", str(clean_dir / "home"))
    monkeypatch.setattr(_jobs, "cpu_budget", lambda: 16)
    monkeypatch.setattr(_jobs, "memory_limit", lambda: SIXTEEN_GIB)
    rec = _Recorder()
    monkeypatch.setattr(f"{QUALITY}.subprocess.run", rec.run)
    monkeypatch.setattr(f"{QUALITY}._run_matrix_pass", rec.matrix_pass)
    monkeypatch.setattr(f"{QUALITY}.shutil.which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(f"{QUALITY}._has_lib_target", lambda *_a: True)
    monkeypatch.setattr(f"{QUALITY}._package_lib_map", lambda *_a: {})
    monkeypatch.setattr(f"{QUALITY}.osv_scanner.run", lambda *_a, **_k: True)
    monkeypatch.setattr(f"{QUALITY}.cargo_flags.run", lambda *_a: 0)

    rc = dispatch._dispatch_to_handler(
        "rust", "quality", CIConfig(_raw={}), extra_env={"RUST_FEATURES": "all"}
    )

    assert rc == 0
    clippy = [jobs for cmd, jobs in rec.seen if cmd[:2] == ["cargo", "clippy"]]
    assert clippy
    assert set(clippy) == {"8"}
    assert all(jobs == "8" for _cmd, jobs in rec.seen)
