# Project:   HyperI CI
# File:      tests/unit/test_workers.py
# Purpose:   Tests for the shared test-worker budget and free-memory readers
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.workers` and the free-memory readers it uses.

CPUs, load and memory are injected, and meminfo / cgroup files are written to
``tmp_path``, so no case depends on the host running the suite.
"""

from pathlib import Path

import pytest

from hyperi_ci import workers
from hyperi_ci.config import CIConfig
from hyperi_ci.memory import (
    GIB,
    MemoryLimit,
    available_memory,
    cgroup_v2_headroom,
    mem_available,
)

_MIB = 1024**2


def _free(gib: float) -> MemoryLimit:
    return MemoryLimit(int(gib * GIB), "test")


def _budget(
    mb: int,
    *,
    cpus: int,
    ci: bool,
    load: float | None = None,
    free: MemoryLimit | None = None,
) -> workers.WorkerBudget:
    return workers.worker_budget(
        mb, cpus=cpus, ci=ci, read_load=lambda: load, read_memory=lambda: free
    )


class TestCpuBound:
    def test_ci_takes_every_cpu(self) -> None:
        assert _budget(1024, cpus=16, ci=True, free=_free(1000)).workers == 16

    def test_ci_ignores_the_load_average(self) -> None:
        assert _budget(1024, cpus=16, ci=True, load=15.0).workers == 16

    def test_off_ci_takes_half_on_an_idle_host(self) -> None:
        assert _budget(1024, cpus=16, ci=False, load=0.0).workers == 8

    def test_off_ci_the_load_average_throttles(self) -> None:
        assert _budget(1024, cpus=16, ci=False, load=12.5).workers == 3

    def test_an_unknown_load_counts_as_idle(self) -> None:
        assert _budget(1024, cpus=16, ci=False, load=None).workers == 8

    @pytest.mark.parametrize(
        ("cpus", "load", "expected"),
        [(8, 40.0, 1), (4, 40.0, 1), (32, 31.0, 4), (32, 27.0, 5), (64, 80.0, 8)],
    )
    def test_a_saturated_host_keeps_an_eighth_of_its_cpus(
        self, cpus: int, load: float, expected: int
    ) -> None:
        assert _budget(1024, cpus=cpus, ci=False, load=load).workers == expected

    def test_the_memory_bound_still_caps_the_floor(self) -> None:
        # 2 GiB * 0.5 / 1 GiB = 1, under the floor of 4
        budget = _budget(1024, cpus=32, ci=False, load=40.0, free=_free(2))
        assert budget.workers == 1

    def test_a_single_cpu_box_gets_one(self) -> None:
        assert _budget(1024, cpus=1, ci=False, load=0.0).workers == 1


class TestMemoryBound:
    def test_ci_plans_on_eighty_percent(self) -> None:
        # 10 GiB * 0.8 / 1 GiB = 8
        budget = _budget(1024, cpus=64, ci=True, free=_free(10))
        assert budget.workers == 8
        assert budget.memory_bound == 8

    def test_off_ci_plans_on_half(self) -> None:
        # 10 GiB * 0.5 / 1 GiB = 5
        assert _budget(1024, cpus=64, ci=False, load=0.0, free=_free(10)).workers == 5

    def test_the_memory_cap_wins_over_the_cpus(self) -> None:
        # 8 GiB * 0.8 / 2 GiB = 3.2, floored to 3, against 32 CPUs
        assert _budget(2048, cpus=32, ci=True, free=_free(8)).workers == 3

    def test_the_cpu_cap_wins_over_plenty_of_memory(self) -> None:
        assert _budget(1024, cpus=4, ci=True, free=_free(256)).workers == 4

    def test_too_little_memory_still_gets_one(self) -> None:
        budget = _budget(4096, cpus=32, ci=True, free=_free(1))
        assert budget.memory_bound == 0
        assert budget.workers == 1

    def test_unreadable_memory_bounds_by_cpus_only(self) -> None:
        budget = _budget(1024, cpus=12, ci=True, free=None)
        assert budget.workers == 12
        assert budget.memory_bound is None
        assert "unreadable" in budget.describe()


class TestForcedWorkers:
    def test_unset_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(workers.WORKERS_ENV, raising=False)
        assert workers.forced_workers() is None

    def test_a_count_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(workers.WORKERS_ENV, "7")
        assert workers.forced_workers() == 7

    def test_junk_warns_and_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(workers, "warn", warnings.append)
        monkeypatch.setenv(workers.WORKERS_ENV, "many")
        assert workers.forced_workers() is None
        assert any(workers.WORKERS_ENV in m for m in warnings)


class TestPerWorkerMb:
    def test_the_shipped_python_default(self) -> None:
        assert (
            workers.per_worker_mb(CIConfig(_raw={}), "test.python.worker_mem_mb")
            == 1024
        )

    def test_the_shipped_rust_default(self) -> None:
        assert (
            workers.per_worker_mb(CIConfig(_raw={}), "test.rust.thread_mem_mb") == 2048
        )

    def test_a_project_value_wins(self) -> None:
        config = CIConfig(_raw={"test": {"rust": {"thread_mem_mb": 4096}}})
        assert workers.per_worker_mb(config, "test.rust.thread_mem_mb") == 4096

    @pytest.mark.parametrize("bad", [0, -5, True, "lots", 1.5])
    def test_an_unusable_value_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, bad: object
    ) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(workers, "warn", warnings.append)
        config = CIConfig(_raw={"test": {"python": {"worker_mem_mb": bad}}})
        assert workers.per_worker_mb(config, "test.python.worker_mem_mb") == 1024
        assert len(warnings) == 1


class TestFreeMemoryReaders:
    def _cgroup(self, tmp_path: Path, limit: str, current: str) -> tuple[Path, Path]:
        root = tmp_path / "cgroup"
        leaf = root / "runner"
        leaf.mkdir(parents=True)
        (leaf / "memory.max").write_text(limit, encoding="utf-8", newline="\n")
        (leaf / "memory.current").write_text(current, encoding="utf-8", newline="\n")
        proc = tmp_path / "proc-self-cgroup"
        proc.write_text("0::/runner\n", encoding="utf-8", newline="\n")
        return root, proc

    def _meminfo(self, tmp_path: Path, available_kib: int) -> Path:
        path = tmp_path / "meminfo"
        path.write_text(
            f"MemTotal:       67108864 kB\nMemAvailable:   {available_kib} kB\n",
            encoding="utf-8",
            newline="\n",
        )
        return path

    def test_mem_available_is_read_in_kib(self, tmp_path: Path) -> None:
        found = mem_available(self._meminfo(tmp_path, 2048))
        assert found is not None
        assert found.limit_bytes == 2048 * 1024

    def test_a_missing_meminfo_reads_as_none(self, tmp_path: Path) -> None:
        assert mem_available(tmp_path / "absent") is None

    def test_cgroup_headroom_is_max_less_current(self, tmp_path: Path) -> None:
        root, proc = self._cgroup(tmp_path, str(4 * GIB), str(GIB))
        found = cgroup_v2_headroom(root, proc)
        assert found is not None
        assert found.limit_bytes == 3 * GIB

    def test_an_unlimited_cgroup_has_no_headroom_figure(self, tmp_path: Path) -> None:
        root, proc = self._cgroup(tmp_path, "max", str(GIB))
        assert cgroup_v2_headroom(root, proc) is None

    def test_an_overcommitted_cgroup_floors_at_zero(self, tmp_path: Path) -> None:
        root, proc = self._cgroup(tmp_path, str(GIB), str(2 * GIB))
        found = cgroup_v2_headroom(root, proc)
        assert found is not None
        assert found.limit_bytes == 0

    def test_the_cgroup_wins_when_lower(self, tmp_path: Path) -> None:
        root, proc = self._cgroup(tmp_path, str(4 * GIB), str(3 * GIB))
        meminfo = self._meminfo(tmp_path, 32 * 1024 * 1024)
        found = available_memory(
            cgroup_root=root, proc_self_cgroup=proc, meminfo=meminfo
        )
        assert found is not None
        assert found.limit_bytes == GIB

    def test_meminfo_wins_when_lower(self, tmp_path: Path) -> None:
        root, proc = self._cgroup(tmp_path, str(64 * GIB), str(GIB))
        meminfo = self._meminfo(tmp_path, 2 * 1024 * 1024)
        found = available_memory(
            cgroup_root=root, proc_self_cgroup=proc, meminfo=meminfo
        )
        assert found is not None
        assert found.limit_bytes == 2 * GIB

    def test_nothing_readable_is_none(self, tmp_path: Path) -> None:
        found = available_memory(
            cgroup_root=tmp_path / "no-cgroup",
            proc_self_cgroup=tmp_path / "no-proc",
            meminfo=tmp_path / "no-meminfo",
        )
        assert found is None

    def test_the_budget_uses_the_injected_files(self, tmp_path: Path) -> None:
        """End to end: a 4 GiB cgroup headroom caps 64 CI CPUs at 3 workers."""
        root, proc = self._cgroup(tmp_path, str(5 * GIB), str(GIB))
        meminfo = self._meminfo(tmp_path, 64 * 1024 * 1024)
        budget = workers.worker_budget(
            1024,
            cpus=64,
            ci=True,
            read_memory=lambda: available_memory(
                cgroup_root=root, proc_self_cgroup=proc, meminfo=meminfo
            ),
        )
        # 4 GiB * 0.8 / 1 GiB = 3.2
        assert budget.workers == 3
