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

from __future__ import annotations

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
    monkeypatch.setattr(cpu, "_own_cgroup_path", lambda: own_path)
    monkeypatch.setattr(cpu, "affinity_cpus", lambda: affinity)


class TestCgroupV2:
    def test_quota_on_the_leaf_caps_the_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cgroup"
        _v2_tree(root, "", "max 100000")
        _v2_tree(root, "kubepods/pod-abc", "400000 100000")
        _point_at(monkeypatch, root, "/kubepods/pod-abc", 32)
        assert cpu.cpu_budget() == 4

    def test_quota_on_an_ancestor_still_binds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A limit set one level up is as real as one on the leaf."""
        root = tmp_path / "cgroup"
        _v2_tree(root, "kubepods", "200000 100000")
        _v2_tree(root, "kubepods/pod-abc", "max 100000")
        _point_at(monkeypatch, root, "/kubepods/pod-abc", 32)
        assert cpu.cpu_budget() == 2

    def test_tightest_of_several_levels_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cgroup"
        _v2_tree(root, "a", "800000 100000")
        _v2_tree(root, "a/b", "300000 100000")
        _point_at(monkeypatch, root, "/a/b", 32)
        assert cpu.cpu_budget() == 3

    def test_no_quota_anywhere_falls_back_to_affinity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cgroup"
        _v2_tree(root, "a/b", "max 100000")
        _point_at(monkeypatch, root, "/a/b", 12)
        assert cpu.cpu_budget() == 12

    def test_fractional_quota_rounds_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 1.5-CPU budget must not become two busy workers."""
        root = tmp_path / "cgroup"
        _v2_tree(root, "a", "150000 100000")
        _point_at(monkeypatch, root, "/a", 32)
        assert cpu.cpu_budget() == 1

    def test_a_sub_one_quota_never_yields_zero_workers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cgroup"
        _v2_tree(root, "a", "50000 100000")
        _point_at(monkeypatch, root, "/a", 32)
        assert cpu.cpu_budget() == 1

    def test_a_malformed_cpu_max_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cgroup"
        _v2_tree(root, "a", "not a quota at all")
        _point_at(monkeypatch, root, "/a", 6)
        assert cpu.cpu_budget() == 6

    def test_a_missing_hierarchy_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_at(monkeypatch, tmp_path / "absent", "/a/b", 9)
        assert cpu.cpu_budget() == 9

    def test_affinity_wins_when_it_is_tighter_than_the_quota(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pinning and a quota are separate limits; the smaller applies."""
        root = tmp_path / "cgroup"
        _v2_tree(root, "a", "1600000 100000")
        _point_at(monkeypatch, root, "/a", 2)
        assert cpu.cpu_budget() == 2


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
        assert cpu._own_cgroup_path() == "/kubepods/pod-abc"

    def test_a_missing_file_reads_as_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cpu, "_PROC_SELF_CGROUP", tmp_path / "absent")
        assert cpu._own_cgroup_path() == "/"

    def test_a_v1_only_file_reads_as_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = tmp_path / "cgroup"
        proc.write_text("12:pids:/legacy\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cpu, "_PROC_SELF_CGROUP", proc)
        assert cpu._own_cgroup_path() == "/"


class TestThisHost:
    def test_the_budget_is_a_usable_number_here(self) -> None:
        """No fabrication: the real host must still answer sanely."""
        assert cpu.cpu_budget() >= 1
        assert cpu.cpu_budget() <= cpu.affinity_cpus()
