# Project:   HyperI CI
# File:      src/hyperi_ci/memory_scope.py
# Purpose:   Run a local check inside a memory-capped systemd scope
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Cap a local ``hyperi-ci check`` at the memory the host has free.

One rustc can need 16 GiB, more than any job count can cap. Without a limit the
host OOM killer picks its victim from everything running, the editor and other
sessions included. Inside a scope with ``MemoryMax`` the shortage stays in the
check, which swaps or is killed while the host carries on.
"""

import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from hyperi_ci.common import info, is_ci, is_linux, run_cmd
from hyperi_ci.memory import GIB, mem_available

# Set in the scoped child so it runs the check instead of scoping again. Set it
# to any value beforehand to run a check without the scope.
SCOPE_ENV = "HYPERCI_MEMORY_SCOPE"

# Left free for the editor, the shell and whatever else the host is running.
_HEADROOM_BYTES = 2 * GIB
_FLOOR_BYTES = 1 * GIB


def scope_cap_bytes(available_bytes: int) -> int:
    """Return the scope's ``MemoryMax``: free memory less headroom, at least 1 GiB."""
    return max(available_bytes - _HEADROOM_BYTES, _FLOOR_BYTES)


def scope_command(
    argv: Sequence[str], cap_bytes: int, environ: Mapping[str, str]
) -> list[str] | None:
    """Return ``argv`` wrapped in a capped user scope, or None where none is possible.

    Args:
        argv: The full command line to re-run, interpreter included.
        cap_bytes: The scope's ``MemoryMax``.
        environ: The environment the check runs in.

    Returns:
        The ``systemd-run`` command, or None in CI, off Linux, inside a scope
        already, or with no user systemd bus to ask for one.

    """
    if is_ci() or not is_linux() or environ.get(SCOPE_ENV):
        return None
    runtime = environ.get("XDG_RUNTIME_DIR")
    systemd_run = shutil.which("systemd-run")
    if not runtime or not systemd_run or not (Path(runtime) / "bus").exists():
        return None
    return [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "-p",
        f"MemoryMax={cap_bytes}",
        f"--setenv={SCOPE_ENV}=1",
        "--",
        *argv,
    ]


def run_in_memory_scope() -> int | None:
    """Re-run this check inside a memory-capped scope.

    Returns:
        The scoped check's exit code, or None when no scope applies and the
        caller runs the check itself.

    """
    available = mem_available()
    if available is None:
        return None
    cap = scope_cap_bytes(available.limit_bytes)
    command = scope_command(sys.orig_argv, cap, os.environ)
    if command is None:
        return None
    info(
        f"check runs in a {cap / GIB:.1f} GiB memory scope ({available.gib:.1f} GiB"
        f" free less {_HEADROOM_BYTES / GIB:.0f} GiB), so a shortage stays in the"
        f" check. {SCOPE_ENV}=off runs it without one."
    )
    return run_cmd(command, check=False).returncode
