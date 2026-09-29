# Project:   HyperI CI
# File:      tests/unit/test_memory_limit.py
# Purpose:   Tests for host memory limit detection under a cgroup limit
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.memory`, against cgroup files in ``tmp_path``."""

from pathlib import Path

from hyperi_ci.memory import (
    GIB,
    cgroup_v1_limit,
    cgroup_v2_limit,
    memory_limit,
    total_ram,
)

# The value cgroup v1 reports for an unlimited cgroup on x86_64 with 4K pages.
V1_UNLIMITED = "9223372036854771712"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _proc_cgroup(tmp_path: Path, own: str) -> Path:
    return _write(tmp_path / "proc-self-cgroup", f"0::{own}\n")


def _meminfo(tmp_path: Path, kib: int) -> Path:
    return _write(
        tmp_path / "meminfo",
        f"MemTotal:       {kib} kB\nMemFree:        1024 kB\n",
    )


class TestCgroupV2:
    def test_namespaced_container_reads_the_root(self, tmp_path: Path) -> None:
        root = tmp_path / "cg"
        _write(root / "memory.max", f"{16 * GIB}\n")
        found = cgroup_v2_limit(root, _proc_cgroup(tmp_path, "/"))
        assert found is not None
        assert found.limit_bytes == 16 * GIB
        assert found.source == f"cgroup v2 {root / 'memory.max'}"

    def test_an_ancestor_limit_binds(self, tmp_path: Path) -> None:
        root = tmp_path / "cg"
        _write(root / "kubepods" / "pod1" / "ctr" / "memory.max", "max\n")
        _write(root / "kubepods" / "pod1" / "memory.max", f"{12 * GIB}\n")
        _write(root / "kubepods" / "memory.max", f"{64 * GIB}\n")
        found = cgroup_v2_limit(root, _proc_cgroup(tmp_path, "/kubepods/pod1/ctr"))
        assert found is not None
        assert found.limit_bytes == 12 * GIB

    def test_max_everywhere_is_unlimited(self, tmp_path: Path) -> None:
        root = tmp_path / "cg"
        _write(root / "user.slice" / "memory.max", "max\n")
        assert cgroup_v2_limit(root, _proc_cgroup(tmp_path, "/user.slice")) is None

    def test_no_cgroup_files(self, tmp_path: Path) -> None:
        assert cgroup_v2_limit(tmp_path / "cg", tmp_path / "missing") is None

    def test_garbage_is_ignored(self, tmp_path: Path) -> None:
        root = tmp_path / "cg"
        _write(root / "memory.max", "lots\n")
        assert cgroup_v2_limit(root, _proc_cgroup(tmp_path, "/")) is None


class TestCgroupV1:
    def test_limit(self, tmp_path: Path) -> None:
        _write(tmp_path / "memory" / "memory.limit_in_bytes", f"{8 * GIB}\n")
        found = cgroup_v1_limit(tmp_path)
        assert found is not None
        assert found.limit_bytes == 8 * GIB
        assert found.source.startswith("cgroup v1 ")

    def test_huge_value_is_unlimited(self, tmp_path: Path) -> None:
        _write(tmp_path / "memory" / "memory.limit_in_bytes", V1_UNLIMITED)
        assert cgroup_v1_limit(tmp_path) is None

    def test_absent(self, tmp_path: Path) -> None:
        assert cgroup_v1_limit(tmp_path) is None


class TestTotalRam:
    def test_meminfo(self, tmp_path: Path) -> None:
        found = total_ram(_meminfo(tmp_path, 64 * 1024 * 1024))
        assert found is not None
        assert found.limit_bytes == 64 * GIB
        assert found.gib == 64.0

    def test_no_meminfo_falls_back_to_sysconf(self, tmp_path: Path) -> None:
        found = total_ram(tmp_path / "missing")
        # Every platform the suite runs on reports physical pages.
        assert found is not None
        assert found.source == "sysconf SC_PHYS_PAGES"
        assert found.limit_bytes > 0


class TestMemoryLimit:
    def test_cgroup_below_ram_wins(self, tmp_path: Path) -> None:
        root = tmp_path / "cg"
        _write(root / "memory.max", f"{16 * GIB}\n")
        found = memory_limit(
            cgroup_root=root,
            proc_self_cgroup=_proc_cgroup(tmp_path, "/"),
            meminfo=_meminfo(tmp_path, 64 * 1024 * 1024),
        )
        assert found is not None
        assert found.limit_bytes == 16 * GIB
        assert found.source.startswith("cgroup v2 ")

    def test_v1_when_v2_is_absent(self, tmp_path: Path) -> None:
        _write(tmp_path / "cg" / "memory" / "memory.limit_in_bytes", f"{4 * GIB}\n")
        found = memory_limit(
            cgroup_root=tmp_path / "cg",
            proc_self_cgroup=tmp_path / "missing",
            meminfo=_meminfo(tmp_path, 64 * 1024 * 1024),
        )
        assert found is not None
        assert found.limit_bytes == 4 * GIB

    def test_unlimited_cgroup_falls_back_to_ram(self, tmp_path: Path) -> None:
        _write(tmp_path / "cg" / "memory" / "memory.limit_in_bytes", V1_UNLIMITED)
        found = memory_limit(
            cgroup_root=tmp_path / "cg",
            proc_self_cgroup=tmp_path / "missing",
            meminfo=_meminfo(tmp_path, 32 * 1024 * 1024),
        )
        assert found is not None
        assert found.limit_bytes == 32 * GIB
        assert found.source.startswith("MemTotal")

    def test_cgroup_above_ram_reports_ram(self, tmp_path: Path) -> None:
        root = tmp_path / "cg"
        _write(root / "memory.max", f"{128 * GIB}\n")
        found = memory_limit(
            cgroup_root=root,
            proc_self_cgroup=_proc_cgroup(tmp_path, "/"),
            meminfo=_meminfo(tmp_path, 32 * 1024 * 1024),
        )
        assert found is not None
        assert found.limit_bytes == 32 * GIB
