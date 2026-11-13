# Project:   HyperI CI
# File:      src/hyperi_ci/check_lock.py
# Purpose:   Serialise local `hyperi-ci check` runs on one host
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Serialise local ``hyperi-ci check`` runs on one host.

Each check sizes its cargo jobs and test workers from the whole host's memory,
so two at once each plan for all of it and the host runs out.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hyperi_ci.common import info, is_ci
from hyperi_ci.upgrade import CACHE_DIR

LOCK_FILE = CACHE_DIR / "check.lock"

# Set while a check holds the lock, so a check it runs (its own test suite
# calling the CLI) proceeds instead of waiting on itself.
HELD_ENV = "HYPERCI_CHECK_LOCK_HELD"


@contextmanager
def check_lock(path: Path | None = None) -> Iterator[None]:
    """Hold the host-wide check lock, waiting while another check holds it.

    A CI runner runs one job, so CI skips the lock. So does a platform with no
    ``fcntl``, and a check started inside one that holds it.

    Args:
        path: The lock file, ``LOCK_FILE`` when None.

    Yields:
        Nothing, once the lock is held.

    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    if is_ci() or os.environ.get(HELD_ENV):
        yield
        return

    path = path or LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8", newline="\n") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip() or "another process"
            info(
                f"Waiting for the check already running on this host ({holder}): "
                "two at once each size their jobs from the whole host's memory"
            )
            fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()} in {Path.cwd()}")
        handle.flush()
        os.environ[HELD_ENV] = "1"
        try:
            yield
        finally:
            del os.environ[HELD_ENV]
            fcntl.flock(handle, fcntl.LOCK_UN)
