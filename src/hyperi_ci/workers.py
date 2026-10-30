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
Where it cannot be read only the CPU bound applies. The bounds live in
:mod:`hyperi_ci.sizing`. This module adds the env override and the config read.
"""

import os

from hyperi_ci.common import warn
from hyperi_ci.config import CIConfig, shipped_default
from hyperi_ci.sizing import usable_mb

WORKERS_ENV = "HYPERCI_TEST_WORKERS"


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
    mb = usable_mb(value)
    if mb is not None:
        return mb
    shipped = int(shipped_default(key))
    warn(f"  {key}: {value!r} is not a positive MiB count, using {shipped}")
    return shipped
