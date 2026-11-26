# Project:   HyperI CI
# File:      tests/unit/test_memwatch.py
# Purpose:   Tests for the test-command memory watch
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.memwatch`."""

import signal
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci import memwatch
from hyperi_ci.config import CIConfig
from hyperi_ci.memwatch import MemoryWatch, WatchSettings, processes, tree

_HEARTBEAT = 30.0


def _config(watch: dict[str, Any]) -> CIConfig:
    return CIConfig(_raw={"test": {"memory_watch": watch}})


def _proc(root: Path, rows: list[tuple[int, int, str, int | None]]) -> Path:
    """Write a fake procfs: (pid, ppid, name, VmRSS in KiB or None for no line)."""
    for pid, ppid, name, rss_kib in rows:
        entry = root / str(pid)
        entry.mkdir()
        (entry / "stat").write_text(f"{pid} ({name}) S {ppid} 1 1 0", encoding="utf-8")
        rss = "" if rss_kib is None else f"VmRSS:\t{rss_kib} kB\n"
        (entry / "status").write_text(f"Name:\t{name}\n{rss}", encoding="utf-8")
    return root


class TestSettings:
    @pytest.mark.parametrize(
        ("watch", "expected"),
        [
            ({}, None),
            ({"enabled": False, "abort_stall_percent": 7}, None),
            ({"enabled": True}, WatchSettings(50, 4)),
            (
                {"enabled": True, "abort_stall_percent": 0, "abort_after_samples": 1},
                WatchSettings(0, 1),
            ),
            ({"enabled": True, "abort_stall_percent": 100}, WatchSettings(100, 4)),
        ],
    )
    def test_reads_the_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        watch: dict[str, Any],
        expected: WatchSettings | None,
    ) -> None:
        monkeypatch.setattr(memwatch, "is_linux", lambda: True)
        assert WatchSettings.from_config(_config(watch)) == expected

    def test_off_away_from_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        said: list[str] = []
        monkeypatch.setattr(memwatch, "is_linux", lambda: False)
        monkeypatch.setattr(memwatch, "info", said.append)
        assert WatchSettings.from_config(_config({"enabled": True})) is None
        assert said == ["  memory watch: Linux only, so it is off on this runner"]

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("abort_stall_percent", 101),
            ("abort_stall_percent", -1),
            ("abort_stall_percent", "50"),
            ("abort_stall_percent", 50.5),
            ("abort_stall_percent", True),
            ("abort_after_samples", 0),
            ("abort_after_samples", None),
            ("abort_below_available_percent", 0),
            ("abort_below_available_percent", 101),
        ],
    )
    def test_rejects_a_bad_threshold(
        self, monkeypatch: pytest.MonkeyPatch, key: str, value: object
    ) -> None:
        monkeypatch.setattr(memwatch, "is_linux", lambda: True)
        with pytest.raises(ValueError, match=f"test.memory_watch.{key} must be"):
            WatchSettings.from_config(_config({"enabled": True, key: value}))

    @pytest.mark.parametrize("block", [True, "on", 1])
    def test_rejects_a_block_that_is_not_a_mapping(self, block: object) -> None:
        config = CIConfig(_raw={"test": {"memory_watch": block}})
        with pytest.raises(ValueError, match="must be a mapping with `enabled: true`"):
            WatchSettings.from_config(config)


class TestProcesses:
    def test_reads_names_parents_and_rss(self, tmp_path: Path) -> None:
        proc = _proc(
            tmp_path,
            [(10, 1, "cargo", 2048), (11, 10, "odd ) name", None), (12, 11, "t", 0)],
        )
        (proc / "self").mkdir()
        (proc / "13").mkdir()  # exited between the listing and the read
        (proc / "14").mkdir()
        (proc / "14" / "stat").write_text("14 (x) S", encoding="utf-8")
        found = sorted(processes(proc), key=lambda p: p.pid)
        assert [(p.pid, p.ppid, p.name, p.rss) for p in found] == [
            (10, 1, "cargo", 2048 * 1024),
            (11, 10, "odd ) name", 0),
            (12, 11, "t", 0),
        ]

    def test_no_procfs_is_empty(self, tmp_path: Path) -> None:
        assert processes(tmp_path / "absent") == []

    def test_tree_takes_descendants_only(self, tmp_path: Path) -> None:
        rows: list[tuple[int, int, str, int | None]] = [
            (10, 1, "a", 1),
            (11, 10, "b", 1),
            (12, 11, "c", 1),
            (20, 1, "d", 1),
        ]
        procs = processes(_proc(tmp_path, rows))
        assert sorted(tree(10, procs)) == [10, 11, 12]
        assert tree(99, procs) == [99]


class _Feed:
    """Cumulative stall seconds that give each heartbeat the share listed."""

    def __init__(self, shares: list[float | None]) -> None:
        self._values: list[float | None] = [0.0]
        total = 0.0
        for share in shares:
            if share is not None:
                total += share / 100 * _HEARTBEAT
            self._values.append(None if share is None else total)

    def __call__(self) -> float | None:
        return self._values.pop(0)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: WatchSettings,
    shares: list[float | None],
    *,
    available: int | None = 2 * 1024**3,
    total: int | None = 16 * 1024**3,
) -> tuple[MemoryWatch, list[tuple[int, int]], list[str], list[str]]:
    proc = _proc(tmp_path, [(10, 1, "cargo", 1024), (11, 10, "nextest", 4 * 1024**2)])
    kills: list[tuple[int, int]] = []
    said: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(memwatch, "info", said.append)
    monkeypatch.setattr(memwatch, "error", errors.append)
    watch = MemoryWatch(
        settings,
        proc=proc,
        read_stall=_Feed(shares),
        read_available=lambda: available,
        read_swap=lambda: None,
        read_total=lambda: total,
        kill=lambda pid, signum: kills.append((pid, signum)),
    )
    for n in range(len(shares)):
        watch((n + 1) * _HEARTBEAT, 10)
    return watch, kills, said, errors


class TestWatch:
    @pytest.mark.parametrize(
        ("settings", "shares", "aborts"),
        [
            (WatchSettings(50, 3), [60, 50, 90], True),
            (WatchSettings(50, 3), [60, 60, 10, 60, 60, 60], True),
            (WatchSettings(50, 3), [60, 60, 49.9, 60, 60], False),
            (WatchSettings(50, 1), [10, 100], True),
            (WatchSettings(0, 1), [100, 100, 100], False),
            (WatchSettings(50, 1), [None, None], False),
        ],
    )
    def test_stops_only_on_a_held_stall(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        settings: WatchSettings,
        shares: list[float | None],
        aborts: bool,
    ) -> None:
        """Each aborting case reaches its count on its last heartbeat."""
        watch, kills, _said, errors = _run(tmp_path, monkeypatch, settings, shares)
        if not aborts:
            assert (watch.aborted, kills, errors) == (False, [], [])
            return
        assert watch.aborted
        assert len(errors) == 1
        assert f"{settings.abort_stall_percent}% or more" in errors[0]
        assert sorted(kills) == [(10, signal.SIGTERM), (11, signal.SIGTERM)]

    @pytest.mark.parametrize(
        ("settings", "available_gib", "total_gib", "aborts"),
        [
            pytest.param(WatchSettings(50, 2), 13.5, 16, False, id="plenty-free"),
            pytest.param(WatchSettings(50, 2), 3, 16, True, id="under-20pct"),
            pytest.param(WatchSettings(50, 2), 13.5, None, True, id="limit-unknown"),
            pytest.param(WatchSettings(50, 2, 100), 13.5, 16, True, id="stall-alone"),
        ],
    )
    def test_a_stall_counts_only_when_memory_is_short(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        settings: WatchSettings,
        available_gib: float,
        total_gib: int | None,
        aborts: bool,
    ) -> None:
        """Reclaim or compaction stalls with RAM free; only thrash loses the runner."""
        watch, kills, _said, errors = _run(
            tmp_path,
            monkeypatch,
            settings,
            [99, 99],
            available=int(available_gib * 1024**3),
            total=None if total_gib is None else total_gib * 1024**3,
        )
        assert watch.aborted is aborts
        assert bool(kills) is aborts
        assert len(errors) == int(aborts)

    def test_kills_what_is_left_a_heartbeat_later(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        watch, kills, said, _errors = _run(
            tmp_path, monkeypatch, WatchSettings(50, 1), [80, 80]
        )
        assert watch.aborted
        assert kills[:2] == [(10, signal.SIGTERM), (11, signal.SIGTERM)]
        assert kills[2:] == [(10, signal.SIGKILL), (11, signal.SIGKILL)]
        assert len(said) == 1

    def test_logs_memory_swap_stall_and_largest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _watch, _kills, said, _errors = _run(
            tmp_path, monkeypatch, WatchSettings(50, 4), [25, None]
        )
        assert said == [
            "  memory watch 30s: 2.0 GiB available, swap unknown used, full stall"
            " 25%; largest: nextest 4.0 GiB, cargo 0.0 GiB",
            "  memory watch 60s: 2.0 GiB available, swap unknown used, no PSI;"
            " largest: nextest 4.0 GiB, cargo 0.0 GiB",
        ]

    def test_a_process_gone_before_the_signal_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _proc(tmp_path, [(10, 1, "cargo", 1), (11, 10, "t", 1)])
        sent: list[int] = []

        def kill(pid: int, _signum: int) -> None:
            if pid == 10:
                raise ProcessLookupError
            sent.append(pid)

        monkeypatch.setattr(memwatch, "info", lambda _m: None)
        monkeypatch.setattr(memwatch, "error", lambda _m: None)
        watch = MemoryWatch(
            WatchSettings(50, 1),
            proc=proc,
            read_stall=_Feed([100]),
            read_available=lambda: None,
            read_swap=lambda: None,
            read_total=lambda: None,
            kill=kill,
        )
        watch(_HEARTBEAT, 10)
        assert (watch.aborted, sent) == (True, [11])
