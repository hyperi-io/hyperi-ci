# Project:   HyperI CI
# File:      src/hyperi_ci/memwatch.py
# Purpose:   Log memory on every heartbeat of a test command, and stop it on a sustained stall
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Memory watch for a long test command on a Linux runner.

A free runner's tests share a cgroup with the runner agent, and swap lets them
outgrow RAM. When they thrash it the agent starves, GitHub loses the runner,
and it keeps no log of the step. Each heartbeat logs memory, swap, the share of
the interval every task spent stalled on memory, and the largest processes. A
stall held for ``abort_after_samples`` heartbeats stops the command, so the
step ends with that log.
"""

import os
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import error, info, is_linux
from hyperi_ci.config import CIConfig
from hyperi_ci.memory import (
    GIB,
    available_memory,
    cgroup_v2_memory_stall,
    swap_used,
)

PROC = Path("/proc")
_TOP = 5
_KEY = "test.memory_watch"


@dataclass(frozen=True, slots=True)
class WatchSettings:
    """When the watch stops a command.

    Attributes:
        abort_stall_percent: Full-stall share of a heartbeat interval that
            counts toward stopping, 0 to never stop.
        abort_after_samples: Heartbeats in a row at or past it that stop the command.

    """

    abort_stall_percent: int
    abort_after_samples: int

    @classmethod
    def from_config(cls, config: CIConfig) -> "WatchSettings | None":
        """Return the settings, or None when the watch is off or not on Linux.

        Raises:
            ValueError: The block is not a mapping, or a threshold is not a
                whole number in range.

        """
        block = config.get(_KEY)
        if block is not None and not isinstance(block, dict):
            raise ValueError(
                f"{_KEY} must be a mapping with `enabled: true`, got {block!r}"
            )
        if not config.setting(f"{_KEY}.enabled"):
            return None
        if not is_linux():
            info("  memory watch: Linux only, so it is off on this runner")
            return None
        percent = _whole(config, "abort_stall_percent", 0, 100)
        samples = _whole(config, "abort_after_samples", 1, None)
        return cls(percent, samples)


def _whole(config: CIConfig, name: str, low: int, high: int | None) -> int:
    value = config.setting(f"{_KEY}.{name}")
    is_int = isinstance(value, int) and not isinstance(value, bool)
    if is_int and value >= low and (high is None or value <= high):
        return value
    bound = f"{low}-{high}" if high is not None else f"{low} or more"
    raise ValueError(f"{_KEY}.{name} must be a whole number {bound}, got {value!r}")


@dataclass(frozen=True, slots=True)
class Process:
    """One process as the watch saw it.

    Attributes:
        pid: Its pid.
        ppid: Its parent's pid.
        name: The kernel's name for it, at most 15 characters.
        rss: Resident memory in bytes, 0 where unreadable.

    """

    pid: int
    ppid: int
    name: str
    rss: int


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _process(proc: Path, pid: str) -> Process | None:
    stat = _read(proc / pid / "stat")
    if stat is None:
        return None
    # The name sits in parentheses and may itself hold spaces or ")".
    head, _, tail = stat.rpartition(")")
    fields = tail.split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    rss = 0
    for line in (_read(proc / pid / "status") or "").splitlines():
        parts = line.split()
        if parts[:1] == ["VmRSS:"] and len(parts) > 1 and parts[1].isdigit():
            rss = int(parts[1]) * 1024
    return Process(int(pid), int(fields[1]), head.partition("(")[2], rss)


def processes(proc: Path = PROC) -> list[Process]:
    """Return every process readable under ``proc``, empty where it is absent."""
    if not proc.is_dir():
        return []
    found: list[Process] = []
    for entry in proc.iterdir():
        if entry.name.isdigit() and (seen := _process(proc, entry.name)) is not None:
            found.append(seen)
    return found


def tree(root: int, procs: Sequence[Process]) -> list[int]:
    """Return ``root`` and every descendant of it in ``procs``, parents first."""
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)
    order = [root]
    for pid in order:
        order.extend(children.get(pid, ()))
    return order


def _gib(value: int | None) -> str:
    return "unknown" if value is None else f"{value / GIB:.1f} GiB"


def _cgroup_full_stall() -> float | None:
    stall = cgroup_v2_memory_stall()
    return None if stall is None else stall.full_seconds


def _available() -> int | None:
    # MemAvailable counts page cache as free and memory.high counts it as used.
    found = available_memory()
    return None if found is None else found.limit_bytes


class MemoryWatch:
    """A ``stream_cmd`` heartbeat that logs memory and stops a stalled command.

    Attributes:
        aborted: The watch stopped the command.

    """

    def __init__(
        self,
        settings: WatchSettings,
        *,
        proc: Path = PROC,
        read_stall: Callable[[], float | None] = _cgroup_full_stall,
        read_available: Callable[[], int | None] = _available,
        read_swap: Callable[[], int | None] = swap_used,
        kill: Callable[[int, int], None] = os.kill,
    ) -> None:
        """Take the stall baseline the first heartbeat measures from.

        Args:
            settings: When to stop the command.
            proc: The procfs mount.
            read_stall: Seconds every task in the cgroup has been stalled on
                memory, cumulative, or None without PSI.
            read_available: Memory available, in bytes.
            read_swap: Swap in use, in bytes.
            kill: Sends a signal to a pid.

        """
        self._settings = settings
        self._proc = proc
        self._read_stall = read_stall
        self._read_available = read_available
        self._read_swap = read_swap
        self._kill = kill
        self._last_elapsed = 0.0
        self._last_stall = read_stall()
        self._run = 0
        self.aborted = False

    def __call__(self, elapsed: float, pid: int) -> None:
        """Log one sample, and stop the command once the stall has held.

        Args:
            elapsed: Seconds since the command started.
            pid: The command's pid.

        """
        procs = processes(self._proc)
        # SIGTERM went out a heartbeat ago, so whatever is left gets SIGKILL.
        if self.aborted:
            self._signal(pid, procs, signal.SIGKILL)
            return
        share = self._stall_share(elapsed)
        largest = sorted(procs, key=lambda p: p.rss, reverse=True)[:_TOP]
        top = ", ".join(f"{p.name} {_gib(p.rss)}" for p in largest)
        stall = "no PSI" if share is None else f"full stall {share:.0f}%"
        info(
            f"  memory watch {elapsed:.0f}s: {_gib(self._read_available())} available,"
            f" swap {_gib(self._read_swap())} used, {stall}; largest: {top}"
        )
        limit = self._settings.abort_stall_percent
        held = limit > 0 and share is not None and share >= limit
        self._run = self._run + 1 if held else 0
        if self._run < self._settings.abort_after_samples:
            return
        error(
            f"memory watch: every task was stalled on memory for {limit}% or more"
            f" of {self._run} heartbeats running, so the test command was stopped"
            " before the runner is lost. Cut the tests' memory or concurrency, or"
            f" raise {_KEY}.abort_stall_percent (0 never stops)."
        )
        self.aborted = True
        self._signal(pid, procs, signal.SIGTERM)

    def _stall_share(self, elapsed: float) -> float | None:
        now = self._read_stall()
        interval = elapsed - self._last_elapsed
        before = self._last_stall
        self._last_stall = now
        self._last_elapsed = elapsed
        if now is None or before is None or interval <= 0:
            return None
        return min(100.0, max(0.0, (now - before) / interval * 100))

    def _signal(self, pid: int, procs: Sequence[Process], signum: int) -> None:
        for target in tree(pid, procs):
            try:
                self._kill(target, signum)
            except (ProcessLookupError, PermissionError):
                continue
