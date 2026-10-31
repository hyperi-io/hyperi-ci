# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/_threads.py
# Purpose:   Cap nextest's test threads by the CPUs and memory free right now
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""nextest test-thread count capped by free memory as well as CPUs.

nextest runs one test process per CPU regardless of memory. Setting
``NEXTEST_TEST_THREADS`` reaches ``cargo nextest`` and ``cargo llvm-cov
nextest`` alike, and a ``--test-threads`` on the command line still beats it.

Precedence: ``NEXTEST_TEST_THREADS`` in the environment, then
``HYPERCI_TEST_WORKERS``, then a ``test-threads`` in the project's nextest
config. nextest ranks our env var above its config file, so the last is
detected and nothing is set.
"""

import os
import tomllib
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path

from hyperi_ci.common import info
from hyperi_ci.config import CIConfig
from hyperi_ci.sizing import WORKERS_ENV, worker_budget
from hyperi_ci.workers import forced_workers, per_worker_mb

THREADS_ENV = "NEXTEST_TEST_THREADS"

_THREAD_MEM_KEY = "test.rust.thread_mem_mb"

_NEXTEST_CONFIG = Path(".config") / "nextest.toml"


def config_sets_threads(root: Path) -> bool:
    """Report whether the project's nextest config sets ``test-threads``.

    Args:
        root: The workspace root.

    Returns:
        True when any profile in ``.config/nextest.toml`` sets it. An
        unreadable file counts as False, and nextest reports it when it runs.

    """
    try:
        data = tomllib.loads(
            (root / _NEXTEST_CONFIG).read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, tomllib.TOMLDecodeError):
        return False
    profiles = data.get("profile")
    if not isinstance(profiles, dict):
        return False
    return any(
        isinstance(profile, dict) and "test-threads" in profile
        for profile in profiles.values()
    )


def nextest_threads_env(
    config: CIConfig,
    environ: MutableMapping[str, str],
    *,
    cwd: Path | None = None,
) -> dict[str, str]:
    """Return the ``NEXTEST_TEST_THREADS`` setting a nextest run should use.

    Args:
        config: Merged CI configuration.
        environ: The environment the run happens in.
        cwd: The workspace root, the process cwd when None.

    Returns:
        ``{"NEXTEST_TEST_THREADS": "<n>"}``, or empty when the count is
        already chosen elsewhere.

    """
    if existing := environ.get(THREADS_ENV):
        info(f"  Test threads: {existing}, from {THREADS_ENV} in the environment")
        return {}

    forced = forced_workers()
    if forced is not None:
        threads = max(1, forced)
        info(f"  Test threads: {threads}, from {WORKERS_ENV}")
        return {THREADS_ENV: str(threads)}

    if config_sets_threads(cwd or Path.cwd()):
        info(f"  Test threads: from test-threads in {_NEXTEST_CONFIG}")
        return {}

    budget = worker_budget(per_worker_mb(config, _THREAD_MEM_KEY))
    info(f"  Test threads: {budget.workers} ({budget.describe()})")
    return {THREADS_ENV: str(budget.workers)}


@contextmanager
def capped_nextest_threads(
    config: CIConfig,
    environ: MutableMapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
) -> Iterator[None]:
    """Set ``NEXTEST_TEST_THREADS`` for the duration of a nextest run.

    Args:
        config: Merged CI configuration.
        environ: The environment to set it in, ``os.environ`` when None.
        cwd: As for :func:`nextest_threads_env`.

    Yields:
        Nothing. The setting is removed again on exit.

    """
    target = os.environ if environ is None else environ
    updates = nextest_threads_env(config, target, cwd=cwd)
    target.update(updates)
    try:
        yield
    finally:
        for key in updates:
            target.pop(key, None)
