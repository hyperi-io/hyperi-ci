# Project:   HyperI CI
# File:      src/hyperi_ci/memory.py
# Purpose:   How much memory this process may actually use, cgroup limit included
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Host memory budget detection.

A 16Gi ARC pod on a 64Gi node reads 64Gi from ``/proc/meminfo`` and is
OOM-killed at 16Gi. This module takes the tightest of the cgroup memory limit
and total RAM, as :mod:`hyperi_ci.cpu` does for CPUs.
"""

import os
from dataclasses import dataclass
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_SELF_CGROUP = Path("/proc/self/cgroup")
MEMINFO = Path("/proc/meminfo")

GIB = 1024**3

# cgroup v1 reports "no limit" as a page-counter maximum near 2**63 bytes.
_V1_UNLIMITED_FLOOR = 1 << 62
_V2_NO_LIMIT = "max"


@dataclass(frozen=True, slots=True)
class MemoryLimit:
    """A memory limit and where it was read from.

    Attributes:
        limit_bytes: The limit in bytes.
        source: The file or API that reported it, for the log line.

    """

    limit_bytes: int
    source: str

    @property
    def gib(self) -> float:
        """The limit in GiB."""
        return self.limit_bytes / GIB


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def own_cgroup_path(proc_self_cgroup: Path) -> str:
    """Return this process's cgroup v2 path, or ``/`` when there is none.

    Args:
        proc_self_cgroup: The ``/proc/self/cgroup`` file to read.

    Returns:
        The path recorded on the ``0::`` line.

    """
    text = _read_text(proc_self_cgroup) or ""
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:].strip() or "/"
    return "/"


def cgroup_v2_limit(
    root: Path = CGROUP_ROOT, proc_self_cgroup: Path = PROC_SELF_CGROUP
) -> MemoryLimit | None:
    """Tightest cgroup v2 ``memory.max`` applying to this process.

    A limit on an ancestor binds as hard as one on the leaf, and a namespaced
    container sees its own limit at the hierarchy root, so every level up to the
    root is read.

    Args:
        root: The unified hierarchy mount.
        proc_self_cgroup: The file naming this process's cgroup.

    Returns:
        The smallest limit found, or None when no level sets one.

    """
    relative = own_cgroup_path(proc_self_cgroup).strip("/")
    directory = root / relative if relative else root
    found: list[MemoryLimit] = []
    while True:
        path = directory / "memory.max"
        raw = _read_text(path)
        if raw and raw != _V2_NO_LIMIT and raw.isdigit() and int(raw) > 0:
            found.append(MemoryLimit(int(raw), f"cgroup v2 {path}"))
        if directory == root or root not in directory.parents:
            break
        directory = directory.parent
    return min(found, key=lambda m: m.limit_bytes) if found else None


def cgroup_v1_limit(root: Path = CGROUP_ROOT) -> MemoryLimit | None:
    """Limit from the legacy cgroup v1 memory controller.

    Args:
        root: The cgroup mount holding the ``memory`` controller.

    Returns:
        The limit, or None when the controller is absent or unlimited.

    """
    path = root / "memory" / "memory.limit_in_bytes"
    raw = _read_text(path)
    if not raw or not raw.isdigit():
        return None
    value = int(raw)
    if value <= 0 or value >= _V1_UNLIMITED_FLOOR:
        return None
    return MemoryLimit(value, f"cgroup v1 {path}")


def total_ram(meminfo: Path = MEMINFO) -> MemoryLimit | None:
    """Physical RAM, from ``/proc/meminfo`` on Linux and ``sysconf`` elsewhere.

    Args:
        meminfo: The meminfo file to read first.

    Returns:
        Total RAM, or None on a platform that reports neither.

    """
    for line in (_read_text(meminfo) or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "MemTotal:" and fields[1].isdigit():
            return MemoryLimit(int(fields[1]) * 1024, f"MemTotal in {meminfo}")
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return MemoryLimit(pages * page_size, "sysconf SC_PHYS_PAGES")


def mem_available(meminfo: Path = MEMINFO) -> MemoryLimit | None:
    """Memory the kernel can hand out without swapping, from ``MemAvailable``.

    Args:
        meminfo: The meminfo file to read.

    Returns:
        The available memory, or None where the file is absent or lacks the
        field, as on macOS.

    """
    for line in (_read_text(meminfo) or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "MemAvailable:" and fields[1].isdigit():
            return MemoryLimit(int(fields[1]) * 1024, f"MemAvailable in {meminfo}")
    return None


def cgroup_v2_headroom(
    root: Path = CGROUP_ROOT, proc_self_cgroup: Path = PROC_SELF_CGROUP
) -> MemoryLimit | None:
    """Tightest ``memory.max`` minus ``memory.current`` over this cgroup's levels.

    Args:
        root: The unified hierarchy mount.
        proc_self_cgroup: The file naming this process's cgroup.

    Returns:
        The smallest headroom, floored at 0, or None when no level sets a
        limit.

    """
    relative = own_cgroup_path(proc_self_cgroup).strip("/")
    directory = root / relative if relative else root
    found: list[MemoryLimit] = []
    while True:
        limit = _read_text(directory / "memory.max")
        current = _read_text(directory / "memory.current")
        if (
            limit
            and limit != _V2_NO_LIMIT
            and limit.isdigit()
            and current
            and current.isdigit()
        ):
            headroom = max(0, int(limit) - int(current))
            found.append(MemoryLimit(headroom, f"cgroup v2 headroom in {directory}"))
        if directory == root or root not in directory.parents:
            break
        directory = directory.parent
    return min(found, key=lambda m: m.limit_bytes) if found else None


def available_memory(
    *,
    cgroup_root: Path = CGROUP_ROOT,
    proc_self_cgroup: Path = PROC_SELF_CGROUP,
    meminfo: Path = MEMINFO,
) -> MemoryLimit | None:
    """Memory free for new work right now, the tighter of host and cgroup.

    Args:
        cgroup_root: The cgroup mount.
        proc_self_cgroup: The file naming this process's cgroup.
        meminfo: The meminfo file.

    Returns:
        The smaller of ``MemAvailable`` and the cgroup v2 headroom, or None
        when neither can be read.

    """
    candidates = [
        mem_available(meminfo),
        cgroup_v2_headroom(cgroup_root, proc_self_cgroup),
    ]
    present = [c for c in candidates if c is not None]
    return min(present, key=lambda m: m.limit_bytes) if present else None


def memory_limit(
    *,
    cgroup_root: Path = CGROUP_ROOT,
    proc_self_cgroup: Path = PROC_SELF_CGROUP,
    meminfo: Path = MEMINFO,
) -> MemoryLimit | None:
    """Memory this process can use before the kernel OOM-kills it.

    The cgroup limit catches a container limit and total RAM catches a host
    with none, so the budget is the smaller of the two.

    Args:
        cgroup_root: The cgroup mount.
        proc_self_cgroup: The file naming this process's cgroup.
        meminfo: The meminfo file.

    Returns:
        The tightest limit, or None when nothing reports one.

    """
    candidates = [
        cgroup_v2_limit(cgroup_root, proc_self_cgroup) or cgroup_v1_limit(cgroup_root),
        total_ram(meminfo),
    ]
    present = [c for c in candidates if c is not None]
    return min(present, key=lambda m: m.limit_bytes) if present else None
