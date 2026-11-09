# Project:   HyperI CI
# File:      src/hyperi_ci/swap.py
# Purpose:   Add swap on free hosted runners so a build short of memory slows rather than dies
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Swap for free runners.

A free GitHub-hosted runner has 15.6 GiB and no way to ask for more, and a
rustc past it is killed. Swap turns that into a slower build. Paid runners are
never touched: the workflow sets ``HYPERCI_RUNNER_MODE`` to ``free`` only in
free mode.
"""

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from hyperi_ci.common import info, is_linux, run_cmd, sudo_prefix, warn
from hyperi_ci.memory import GIB

MODE_ENV = "HYPERCI_RUNNER_MODE"
SWAP_FILE = "hyperi-ci.swap"
PROC_SWAPS = Path("/proc/swaps")

_MAX_TOTAL_GIB = 32
_DISK_SHARE = 4


def _swap_rows(proc_swaps: Path) -> list[list[str]]:
    try:
        lines = proc_swaps.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line.split() for line in lines[1:] if line.strip()]


def swap_total_bytes(proc_swaps: Path = PROC_SWAPS) -> int:
    """Return the summed size of every active swap area, 0 when none or unreadable."""
    rows = _swap_rows(proc_swaps)
    return sum(int(r[2]) * 1024 for r in rows if len(r) >= 3 and r[2].isdigit())


def swap_active(path: Path, proc_swaps: Path = PROC_SWAPS) -> bool:
    """Return whether ``path`` is an active swap area."""
    return any(row[0] == str(path) for row in _swap_rows(proc_swaps))


def target_total_bytes(ram_bytes: int, free_disk_bytes: int) -> int:
    """Return the swap a free runner should end up with.

    Twice its RAM, at most 32 GiB and at most a quarter of the free disk, so
    the build keeps its working space.

    Args:
        ram_bytes: The runner's memory.
        free_disk_bytes: Free space on the filesystem the swap file goes on.

    Returns:
        The target in bytes.

    """
    return min(2 * ram_bytes, _MAX_TOTAL_GIB * GIB, free_disk_bytes // _DISK_SHARE)


def swap_commands(path: Path, gib: int) -> list[list[str]]:
    """Return the commands that create and enable a ``gib`` GiB swap file."""
    sudo = sudo_prefix()
    return [
        [*sudo, "fallocate", "-l", f"{gib}G", str(path)],
        [*sudo, "chmod", "600", str(path)],
        [*sudo, "mkswap", str(path)],
        [*sudo, "swapon", str(path)],
    ]


def ensure_free_runner_swap(
    ram_bytes: int,
    environ: Mapping[str, str] | None = None,
    *,
    proc_swaps: Path = PROC_SWAPS,
) -> int | None:
    """Add swap up to the target on a free Linux runner.

    A failed command warns and the build runs without the extra swap.

    Args:
        ram_bytes: The runner's memory.
        environ: The environment, ``os.environ`` if None.
        proc_swaps: The kernel's swap table.

    Returns:
        The active swap in bytes afterwards, or None when this is not a free
        Linux runner and nothing was looked at.

    """
    env = os.environ if environ is None else environ
    temp = env.get("RUNNER_TEMP")
    if env.get(MODE_ENV) != "free" or not temp or not is_linux():
        return None
    path = Path(temp) / SWAP_FILE
    existing = swap_total_bytes(proc_swaps)
    if swap_active(path, proc_swaps):
        return existing
    try:
        wanted = target_total_bytes(ram_bytes, shutil.disk_usage(temp).free)
    except OSError as exc:
        warn(f"  swap: cannot read free space on {temp}, building without it: {exc}")
        return existing
    gib = int((wanted - existing) // GIB)
    if gib < 1:
        return existing
    for cmd in swap_commands(path, gib):
        try:
            result = run_cmd(cmd, check=False, capture=True, merge_stderr=True)
            output = (result.stdout or "").strip()
            failed = result.returncode != 0
        except OSError as exc:
            output, failed = str(exc), True
        if failed:
            warn(f"  swap: `{' '.join(cmd)}` failed, building without it: {output}")
            run_cmd([*sudo_prefix(), "rm", "-f", str(path)], check=False, capture=True)
            return existing
    total = swap_total_bytes(proc_swaps)
    info(f"swap: added {gib} GiB at {path}, {total / GIB:.1f} GiB active")
    return total
