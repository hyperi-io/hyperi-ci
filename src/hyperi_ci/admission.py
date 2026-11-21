# Project:   HyperI CI
# File:      src/hyperi_ci/admission.py
# Purpose:   The memory and CPU grant a shared host's admission gives a check
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The memory and CPU grant ``hyperi-ai remote run`` gives a job it admits.

Admission leases each job its share of a shared host and exports the grant. A
check holding one takes no host-wide lock, stays in the caller's scope and sizes
to the grant. Stdlib only, for the pytest plugin.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from hyperi_ci.memory import MemoryLimit

MEM_ENV = "HYPERI_BUDGET_MEM_MB"
CPUS_ENV = "HYPERI_BUDGET_CPUS"

_MIB = 1024**2


@dataclass(frozen=True, slots=True)
class AdmittedBudget:
    """The share of a shared host one job was granted.

    Attributes:
        memory_bytes: Memory granted.
        cpus: CPUs granted.

    """

    memory_bytes: int
    cpus: int

    def cap(self, limit: MemoryLimit | None) -> MemoryLimit:
        """Return the smaller of the grant and ``limit``, the grant when ``limit`` is None."""
        if limit is not None and limit.limit_bytes <= self.memory_bytes:
            return limit
        return MemoryLimit(self.memory_bytes, MEM_ENV)


def _positive_int(raw: str | None) -> int | None:
    try:
        value = int((raw or "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


def admitted_budget(environ: Mapping[str, str] | None = None) -> AdmittedBudget | None:
    """Return the grant in ``environ``, None unless both values are positive integers.

    Args:
        environ: The environment to read, ``os.environ`` when None.

    Returns:
        The grant, or None, which keeps the host-wide lock and scope.

    """
    env = os.environ if environ is None else environ
    memory_mb = _positive_int(env.get(MEM_ENV))
    cpus = _positive_int(env.get(CPUS_ENV))
    if memory_mb is None or cpus is None:
        return None
    return AdmittedBudget(memory_mb * _MIB, cpus)
