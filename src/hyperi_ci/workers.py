# Project:   HyperI CI
# File:      src/hyperi_ci/workers.py
# Purpose:   Size a parallel test run by the CPUs and the memory free right now
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Test worker count bounded by idle CPUs and by free memory.

One worker per CPU OOMs a shared host when each worker holds a GiB, so the
count is ``max(1, min(cpu_bound, memory_bound))``:

- On CI: ``cpu_bound = cpus`` and ``memory_bound = floor(available * 0.8 /
  per_worker)``.
- Elsewhere: ``cpu_bound = max(cpus / 8, min(cpus / 2, cpus - loadavg))`` and
  ``memory_bound = floor(available * 0.5 / per_worker)``.

``available`` is ``MemAvailable`` or the cgroup v2 headroom, whichever is lower.
Where it cannot be read only the CPU bound applies.
"""

import math
import os
from collections.abc import Callable
from dataclasses import dataclass

from hyperi_ci.common import debug, is_ci, warn
from hyperi_ci.config import CIConfig, shipped_default
from hyperi_ci.cpu import cpu_budget
from hyperi_ci.memory import MemoryLimit, available_memory

WORKERS_ENV = "HYPERCI_TEST_WORKERS"

_MIB = 1024**2

# Share of free memory the workers may plan on. A CI runner runs nothing else,
# a developer box does.
_CI_MEMORY_SHARE = 0.8
_LOCAL_MEMORY_SHARE = 0.5

_LOCAL_CPU_DIVISOR = 2
# A spike in the 1-minute load must not drop a 32-core box to one worker.
_LOCAL_CPU_FLOOR_DIVISOR = 8


@dataclass(frozen=True, slots=True)
class WorkerBudget:
    """A worker count and the bounds that produced it.

    Attributes:
        workers: Workers to run, at least 1.
        cpu_bound: Workers the CPUs allow.
        memory_bound: Workers the free memory allows, None when unreadable.
        memory: Free memory read from the host, None when unreadable.
        per_worker_mb: Memory planned per worker.

    """

    workers: int
    cpu_bound: int
    memory_bound: int | None
    memory: MemoryLimit | None
    per_worker_mb: int

    def describe(self) -> str:
        """Return the bounds as one log fragment."""
        if self.memory is None:
            return f"CPU bound {self.cpu_bound}, free memory unreadable"
        return (
            f"CPU bound {self.cpu_bound}, memory bound {self.memory_bound} "
            f"({self.memory.gib:.1f} GiB free from {self.memory.source}, "
            f"{self.per_worker_mb} MiB per worker)"
        )


def forced_workers() -> int | None:
    """Read a worker count forced for this run with ``HYPERCI_TEST_WORKERS``.

    Returns:
        The requested count, 0 for serial, or None when unset or unparseable.

    """
    raw = os.environ.get(WORKERS_ENV, "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        warn(f"{WORKERS_ENV}: '{raw}' is not a number - ignoring it")
        return None


def per_worker_mb(config: CIConfig, key: str) -> int:
    """Return a per-worker memory setting, the shipped value when unusable.

    Args:
        config: Merged CI configuration.
        key: The dotted config key, declared in defaults.yaml.

    Returns:
        A positive MiB count.

    """
    value = config.setting(key)
    # bool is an int subclass, and HYPERCI_* parses "1" as True.
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    shipped = int(shipped_default(key))
    warn(f"  {key}: {value!r} is not a positive MiB count, using {shipped}")
    return shipped


def load_average() -> float | None:
    """Return the 1-minute load average, None where the platform has none."""
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def cpu_bound(cpus: int, load: float | None, *, ci: bool) -> int:
    """Return how many workers the CPUs allow.

    Args:
        cpus: The process's CPU budget.
        load: The 1-minute load average, None when unknown.
        ci: Whether this is a CI runner.

    Returns:
        ``cpus`` on CI. Elsewhere the smaller of half the CPUs and the idle
        CPUs, but never below an eighth of the CPUs. Under 8 CPUs that floor
        is 0.

    """
    if ci:
        return cpus
    idle = cpus if load is None else max(0, math.floor(cpus - load))
    floor = cpus // _LOCAL_CPU_FLOOR_DIVISOR
    return max(floor, min(cpus // _LOCAL_CPU_DIVISOR, idle))


def memory_bound(available: MemoryLimit, mb_per_worker: int, *, ci: bool) -> int:
    """Return how many workers the free memory holds.

    Args:
        available: Free memory.
        mb_per_worker: Memory planned per worker.
        ci: Whether this is a CI runner.

    Returns:
        ``floor(available * share / per_worker)``, which may be 0.

    """
    share = _CI_MEMORY_SHARE if ci else _LOCAL_MEMORY_SHARE
    usable = int(available.limit_bytes * share)
    return usable // (mb_per_worker * _MIB)


def worker_budget(
    mb_per_worker: int,
    *,
    cpus: int | None = None,
    ci: bool | None = None,
    read_load: Callable[[], float | None] = load_average,
    read_memory: Callable[[], MemoryLimit | None] = available_memory,
) -> WorkerBudget:
    """Return the worker count the CPUs and the free memory both allow.

    Args:
        mb_per_worker: Memory planned per worker.
        cpus: The CPU budget, read from the host when None.
        ci: Whether this is a CI runner, detected when None.
        read_load: Reads the 1-minute load average, None where it cannot.
        read_memory: Reads free memory, None where it cannot.

    Returns:
        The budget, with at least 1 worker.

    """
    on_ci = is_ci() if ci is None else ci
    cpu_cap = cpu_bound(
        cpu_budget() if cpus is None else cpus,
        None if on_ci else read_load(),
        ci=on_ci,
    )
    available = read_memory()
    mem_cap: int | None = None
    if available is None:
        debug("  Worker budget: free memory unreadable, bounding by CPUs only")
    else:
        mem_cap = memory_bound(available, mb_per_worker, ci=on_ci)
    workers = cpu_cap if mem_cap is None else min(cpu_cap, mem_cap)
    return WorkerBudget(max(1, workers), cpu_cap, mem_cap, available, mb_per_worker)
