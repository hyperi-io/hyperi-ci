# Project:   HyperI CI
# File:      tests/unit/test_cpu_budget.py
# Purpose:   Tests for host CPU budget detection under a cgroup quota
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.cpu`.

Real cgroup files in ``tmp_path``, with the module's hierarchy roots pointed
at them - the case that matters is a 4-CPU container on a many-core node,
which no amount of core counting gets right on its own.
"""

from pathlib import Path

import pytest

from hyperi_ci import cpu


def _v2_tree(root: Path, relative: str, cpu_max: str | None) -> Path:
    """Build a cgroup v2 directory, optionally carrying a ``cpu.max``."""
    directory = root / relative if relative else root
    directory.mkdir(parents=True, exist_ok=True)
    if cpu_max is not None:
        (directory / "cpu.max").write_text(cpu_max, encoding="utf-8", newline="\n")
    return directory


def _point_at(
    monkeypatch: pytest.MonkeyPatch, root: Path, own_path: str, affinity: int
) -> None:
    """Redirect the module at a fabricated hierarchy and affinity mask."""
    monkeypatch.setattr(cpu, "_CGROUP_ROOT", root)
    monkeypatch.setattr(cpu, "own_cgroup_path", lambda _proc: own_path)
    monkeypatch.setattr(cpu, "affinity_cpus", lambda: affinity)


class TestCgroupV2:
    @pytest.mark.parametrize(
        ("levels", "own_path", "affinity", "expected"),
        [
            pytest.param(
                [("", "max 100000"), ("kubepods/pod-abc", "400000 100000")],
                "/kubepods/pod-abc",
                32,
                4,
                id="quota-on-the-leaf-caps-the-budget",
            ),
            pytest.param(
                [("kubepods", "200000 100000"), ("kubepods/pod-abc", "max 100000")],
                "/kubepods/pod-abc",
                32,
                2,
                id="quota-on-an-ancestor-still-binds",
            ),
            pytest.param(
                [("a", "800000 100000"), ("a/b", "300000 100000")],
                "/a/b",
                32,
                3,
                id="tightest-of-several-levels-wins",
            ),
            pytest.param(
                [("a/b", "max 100000")],
                "/a/b",
                12,
                12,
                id="no-quota-anywhere-falls-back-to-affinity",
            ),
            pytest.param(
                [("a", "150000 100000")], "/a", 32, 1, id="fractional-quota-rounds-down"
            ),
            pytest.param(
                [("a", "50000 100000")],
                "/a",
                32,
                1,
                id="a-sub-one-quota-never-yields-zero-workers",
            ),
            pytest.param(
                [("a", "not a quota at all")],
                "/a",
                6,
                6,
                id="a-malformed-cpu-max-is-ignored",
            ),
            pytest.param([], "/a/b", 9, 9, id="a-missing-hierarchy-is-ignored"),
            pytest.param(
                [("a", "1600000 100000")],
                "/a",
                2,
                2,
                id="affinity-wins-when-tighter-than-the-quota",
            ),
        ],
    )
    def test_the_budget(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        levels: list[tuple[str, str]],
        own_path: str,
        affinity: int,
        expected: int,
    ) -> None:
        root = tmp_path / "cgroup"
        for relative, cpu_max in levels:
            _v2_tree(root, relative, cpu_max)
        _point_at(monkeypatch, root, own_path, affinity)
        assert cpu.cpu_budget() == expected


class TestCgroupV1:
    def test_quota_is_read_from_the_legacy_controller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cgroup"
        controller = root / "cpu"
        controller.mkdir(parents=True)
        (controller / "cpu.cfs_quota_us").write_text(
            "300000", encoding="utf-8", newline="\n"
        )
        (controller / "cpu.cfs_period_us").write_text(
            "100000", encoding="utf-8", newline="\n"
        )
        _point_at(monkeypatch, root, "/", 32)
        assert cpu.cpu_budget() == 3

    def test_the_unlimited_sentinel_is_not_a_quota(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cgroup"
        controller = root / "cpu"
        controller.mkdir(parents=True)
        (controller / "cpu.cfs_quota_us").write_text(
            "-1", encoding="utf-8", newline="\n"
        )
        (controller / "cpu.cfs_period_us").write_text(
            "100000", encoding="utf-8", newline="\n"
        )
        _point_at(monkeypatch, root, "/", 7)
        assert cpu.cpu_budget() == 7


class TestOwnCgroupPath:
    def test_the_v2_line_is_the_one_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = tmp_path / "cgroup"
        proc.write_text(
            "12:pids:/legacy\n0::/kubepods/pod-abc\n", encoding="utf-8", newline="\n"
        )
        monkeypatch.setattr(cpu, "_PROC_SELF_CGROUP", proc)
        assert cpu.own_cgroup_path(cpu._PROC_SELF_CGROUP) == "/kubepods/pod-abc"

    def test_a_missing_file_reads_as_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cpu, "_PROC_SELF_CGROUP", tmp_path / "absent")
        assert cpu.own_cgroup_path(cpu._PROC_SELF_CGROUP) == "/"

    def test_a_v1_only_file_reads_as_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = tmp_path / "cgroup"
        proc.write_text("12:pids:/legacy\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cpu, "_PROC_SELF_CGROUP", proc)
        assert cpu.own_cgroup_path(cpu._PROC_SELF_CGROUP) == "/"


class TestCpuModel:
    @pytest.mark.parametrize(
        ("cpuinfo", "want"),
        [
            pytest.param(
                "processor\t: 0\nmodel name\t: AMD EPYC 9R14\nprocessor\t: 1\n"
                "model name\t: AMD EPYC 9R14\n",
                "AMD EPYC 9R14",
                id="x86_model_name",
            ),
            pytest.param(
                "processor\t: 0\nBogoMIPS\t: 2000.00\nCPU implementer\t: 0xc0\n"
                "CPU part\t: 0xac4\n",
                "implementer 0xc0 part 0xac4",
                id="arm64_ids",
            ),
            pytest.param("processor\t: 0\nCPU part\t: 0xac4\n", None, id="arm64_half"),
            pytest.param("", None, id="empty"),
        ],
    )
    def test_reads_the_model(
        self, tmp_path: Path, cpuinfo: str, want: str | None
    ) -> None:
        path = tmp_path / "cpuinfo"
        path.write_text(cpuinfo, encoding="utf-8", newline="\n")
        assert cpu.cpu_model(path) == want

    def test_a_missing_file_is_none(self, tmp_path: Path) -> None:
        assert cpu.cpu_model(tmp_path / "absent") is None


class TestThisHost:
    def test_the_budget_is_a_usable_number_here(self) -> None:
        """No fabrication: the real host must still answer sanely."""
        assert cpu.cpu_budget() >= 1
        assert cpu.cpu_budget() <= cpu.affinity_cpus()
