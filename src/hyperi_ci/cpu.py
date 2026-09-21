# Project:   HyperI CI
# File:      src/hyperi_ci/cpu.py
# Purpose:   How many CPUs this process may actually use, cgroup quota included
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Host CPU budget detection.

``os.cpu_count()`` reports the machine's cores, which is the wrong answer
inside a CPU-limited container: a 4-CPU ARC pod on a 32-core node reports
32. This module takes the tightest of the affinity mask and the cgroup CPU
quota so a spawned worker pool matches the budget the scheduler will
actually honour.
"""

from __future__ import annotations

import os
from pathlib import Path

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")

# cgroup v1 writes a sentinel rather than omitting the file when no quota is
# set; v2 writes the literal string "max".
_V1_NO_QUOTA = -1
_V2_NO_QUOTA = "max"


def _read_int(path: Path) -> int | None:
    """Read a single integer from a sysfs file.

    Args:
        path: File to read.

    Returns:
        The integer, or None when the file is absent, unreadable or not a
        number.

    """
    try:
        return int(path.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return None


def _quota_from_v2(directory: Path) -> float | None:
    """Read one cgroup v2 ``cpu.max`` file as a CPU count.

    Args:
        directory: A cgroup directory under the unified hierarchy.

    Returns:
        CPUs allowed by this cgroup, or None when it sets no quota.

    """
    try:
        raw = (directory / "cpu.max").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parts = raw.split()
    if len(parts) != 2 or parts[0] == _V2_NO_QUOTA:
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def _own_cgroup_path() -> str:
    """Return this process's cgroup v2 path, or ``/`` when there is none.

    Returns:
        The path recorded on the ``0::`` line of ``/proc/self/cgroup``.

    """
    try:
        lines = _PROC_SELF_CGROUP.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return "/"
    for line in lines:
        if line.startswith("0::"):
            return line[3:].strip() or "/"
    return "/"


def _cgroup_v2_cpus() -> float | None:
    """Tightest cgroup v2 CPU quota applying to this process.

    A quota set on an ancestor binds just as hard as one set on the leaf, and
    a namespaced container sees its own limit at the hierarchy root, so every
    level from this process's cgroup up to the root is checked.

    Returns:
        CPUs allowed, or None when no level sets a quota.

    """
    relative = _own_cgroup_path().strip("/")
    directory = _CGROUP_ROOT / relative if relative else _CGROUP_ROOT
    limits: list[float] = []
    while True:
        found = _quota_from_v2(directory)
        if found is not None:
            limits.append(found)
        if directory == _CGROUP_ROOT or _CGROUP_ROOT not in directory.parents:
            break
        directory = directory.parent
    return min(limits) if limits else None


def _cgroup_v1_cpus() -> float | None:
    """CPU quota from the legacy cgroup v1 cpu controller.

    Returns:
        CPUs allowed, or None when the controller is absent or unlimited.

    """
    quota = _read_int(_CGROUP_ROOT / "cpu" / "cpu.cfs_quota_us")
    period = _read_int(_CGROUP_ROOT / "cpu" / "cpu.cfs_period_us")
    if quota is None or period is None:
        return None
    if quota == _V1_NO_QUOTA or quota <= 0 or period <= 0:
        return None
    return quota / period


def affinity_cpus() -> int:
    """CPUs this process is permitted to be scheduled on.

    Returns:
        The size of the affinity mask, falling back to the machine's core
        count where the platform has no affinity API.

    """
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        try:
            return max(1, len(getaffinity(0)))
        except OSError:
            pass
    return max(1, os.cpu_count() or 1)


def cgroup_cpus() -> float | None:
    """CPU quota imposed by the cgroup this process runs in.

    Returns:
        Fractional CPUs allowed, or None when nothing limits this process.

    """
    return _cgroup_v2_cpus() or _cgroup_v1_cpus()


def cpu_budget() -> int:
    """CPUs this process can actually use.

    The affinity mask catches pinning, the cgroup quota catches a container
    CPU limit, and neither implies the other -- so the budget is the smaller
    of the two, floored at one.

    Returns:
        A CPU count of at least 1.

    """
    budget = affinity_cpus()
    quota = cgroup_cpus()
    if quota is not None:
        budget = min(budget, int(quota))
    return max(1, budget)
