# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/parallel.py
# Purpose:   Decide whether, and how wide, to run pytest across xdist workers
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""pytest worker-count resolution.

Unless ``test.python.parallel`` says otherwise, the worker count comes from the
host's idle CPUs and free memory (:mod:`hyperi_ci.workers`). A project that
passes its own ``-n`` is left alone.

When hyperi-ci supplies ``-n`` it also supplies ``--dist worksteal`` unless the
project chose its own distribution mode.
"""

import os
import re
from pathlib import Path

from hyperi_ci.common import info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.python.pytest_args import project_args
from hyperi_ci.sizing import worker_budget
from hyperi_ci.workers import WORKERS_ENV, forced_workers, per_worker_mb

# Past 16 workers collection cost cancels the gain: hyperi-ci's own 2598-test
# suite takes 23.6s at 8, 23.4s at 16 and 23.8s at 32. An explicit number
# overrides it.
_MAX_AUTO_WORKERS = 16

_WORKER_MEM_KEY = "test.python.worker_mem_mb"

# In a project's own pytest arguments these mean it already chose, including
# turning xdist off.
_OWN_PARALLEL_FLAGS = ("-n", "--numprocesses")
_XDIST_DISABLED = ("no:xdist", "no:xdist.plugin")

# xdist's `-d` is shorthand for `--dist=load` and overrides any `--dist`.
_OWN_DIST_FLAGS = ("--dist", "-d")

_DIST_MODE = "worksteal"

# Older pytest-xdist rejects `--dist worksteal` as a usage error and runs nothing.
_WORKSTEAL_MIN_XDIST = (3, 2)

_XDIST_PLUGIN_LINE = re.compile(r"pytest-xdist-(\d+)\.(\d+)")


def _tokens_claim_parallelism(tokens: list[str]) -> bool:
    """Report whether a pytest argument list already settles worker count.

    Args:
        tokens: Individual pytest arguments, already shell-split.

    Returns:
        True when the arguments set ``-n`` / ``--numprocesses``, or disable
        the xdist plugin outright.

    """
    for index, token in enumerate(tokens):
        if token in _OWN_PARALLEL_FLAGS:
            return True
        if token.startswith("-n") and len(token) > 2:
            return True
        if token.startswith("--numprocesses="):
            return True
        if token == "-p" and index + 1 < len(tokens):  # noqa: S105 -- a CLI arg token, not a password
            if tokens[index + 1] in _XDIST_DISABLED:
                return True
        if token.startswith("-p") and token[2:] in _XDIST_DISABLED:
            return True
    return False


def _tokens_claim_dist(tokens: list[str]) -> bool:
    """Report whether a pytest argument list already picks a distribution mode.

    Args:
        tokens: Individual pytest arguments, already shell-split.

    Returns:
        True when the arguments set ``--dist`` or xdist's ``-d`` shorthand.

    """
    return any(
        token in _OWN_DIST_FLAGS or token.startswith("--dist=") for token in tokens
    )


def project_sets_own_workers(args: list[str], root: Path | None = None) -> bool:
    """Report whether the project has already chosen its own worker count.

    Checks the arguments hyperi-ci is about to pass, the ``addopts`` pytest
    reads from the project's own configuration, and ``PYTEST_ADDOPTS``.

    Args:
        args: pytest arguments hyperi-ci has assembled so far.
        root: Project directory; defaults to the working directory.

    Returns:
        True when any of those sources sets ``-n`` or disables xdist.

    """
    return _tokens_claim_parallelism(project_args(args, root))


def project_sets_own_dist(args: list[str], root: Path | None = None) -> bool:
    """Report whether the project has already chosen its xdist distribution mode.

    Reads the same sources as :func:`project_sets_own_workers`.

    Args:
        args: pytest arguments hyperi-ci has assembled so far.
        root: Project directory; defaults to the working directory.

    Returns:
        True when any of those sources sets ``--dist`` or ``-d``.

    """
    return _tokens_claim_dist(project_args(args, root))


def _resolve(config: CIConfig) -> tuple[int | None, str]:
    """Resolve the worker count and say where it came from.

    Args:
        config: Merged CI configuration.

    Returns:
        A worker count of at least 1, or None for serial, and the source for
        the log line.

    """
    forced = forced_workers()
    if forced is not None:
        return (forced or None), WORKERS_ENV

    raw = config.setting("test.python.parallel")
    if raw is None or raw is True:
        return _auto(config)
    if raw is False:
        return None, "test.python.parallel"
    if isinstance(raw, int):
        return (raw if raw >= 1 else None), "test.python.parallel"
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "auto"}:
            return _auto(config)
        if lowered in {"false", "off", ""}:
            return None, "test.python.parallel"
        try:
            return max(1, int(lowered)), "test.python.parallel"
        except ValueError:
            pass
    warn(
        f"test.python.parallel: unknown value {raw!r} - expected "
        f"true / false / auto / a worker count; running serial"
    )
    return None, "test.python.parallel"


def requested_workers(config: CIConfig) -> int | None:
    """Resolve how many pytest workers the project has asked for.

    ``test.python.parallel`` takes ``false`` (serial), ``true`` or ``auto``
    (derive from the host), or an explicit worker count. Unset means ``auto``,
    and :func:`parallel_args` runs serial instead when pytest-xdist is absent.

    Args:
        config: Merged CI configuration.

    Returns:
        A worker count of at least 1, or None for a serial run.

    """
    return _resolve(config)[0]


def parallel_unset(config: CIConfig) -> bool:
    """Report whether neither the config nor the environment chose a mode.

    Args:
        config: Merged CI configuration.

    Returns:
        True when ``test.python.parallel`` is unset and ``HYPERCI_TEST_WORKERS``
        is not set.

    """
    if os.environ.get(WORKERS_ENV, "").strip():
        return False
    return config.setting("test.python.parallel") is None


def _auto(config: CIConfig) -> tuple[int, str]:
    """Return the host-derived worker count and its bounds."""
    budget = worker_budget(per_worker_mb(config, _WORKER_MEM_KEY))
    return min(budget.workers, _MAX_AUTO_WORKERS), budget.describe()


def auto_workers(config: CIConfig) -> int:
    """Worker count derived from the host, capped at a useful width.

    Args:
        config: Merged CI configuration, for ``test.python.worker_mem_mb``.

    Returns:
        A worker count of at least 1.

    """
    return _auto(config)[0]


def xdist_version(pytest_cmd: list[str]) -> tuple[int, int] | None:
    """Report which pytest-xdist the pytest that will run the suite loads.

    Probes the resolved command, not the ambient interpreter.

    Args:
        pytest_cmd: The resolved pytest invocation, without test arguments.

    Returns:
        ``(major, minor)`` of the registered pytest-xdist plugin, ``(0, 0)``
        when it is registered under a version this cannot read, or None when
        it is absent.

    """
    from hyperi_ci.common import run_cmd

    try:
        result = run_cmd([*pytest_cmd, "-VV"], check=False, capture=True)
    except (OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout or ""
    if "pytest-xdist" not in output:
        return None
    match = _XDIST_PLUGIN_LINE.search(output)
    if match is None:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def _dist_args(args: list[str], xdist: tuple[int, int]) -> list[str]:
    """Return the ``--dist`` arguments to pair with a hyperi-ci supplied ``-n``.

    Args:
        args: pytest arguments hyperi-ci has assembled so far.
        xdist: ``(major, minor)`` of the pytest-xdist that will run.

    Returns:
        ``["--dist", "worksteal"]``, or an empty list when the project picks
        its own mode or its xdist predates worksteal.

    """
    if project_sets_own_dist(args):
        info("  Test distribution: project sets its own --dist, leaving it alone")
        return []
    if xdist < _WORKSTEAL_MIN_XDIST:
        minimum = ".".join(str(part) for part in _WORKSTEAL_MIN_XDIST)
        found = ".".join(str(part) for part in xdist) if any(xdist) else "unknown"
        warn(
            f"  Test distribution: {_DIST_MODE} needs pytest-xdist >= {minimum}, "
            f"found {found} - keeping xdist's default mode; upgrade pytest-xdist"
        )
        return []
    return ["--dist", _DIST_MODE]


def parallel_args(
    config: CIConfig, args: list[str], pytest_cmd: list[str]
) -> list[str]:
    """Return the ``-n`` and ``--dist`` arguments to append, if any.

    Args:
        config: Merged CI configuration.
        args: pytest arguments hyperi-ci has assembled so far.
        pytest_cmd: The resolved pytest invocation, without test arguments.

    Returns:
        ``["-n", "<count>"]``, followed by ``["--dist", "worksteal"]`` unless
        the project sets its own mode, or an empty list to run serial.

    """
    workers, source = _resolve(config)
    if workers is None:
        return []
    if project_sets_own_workers(args):
        info("  Test parallelism: project sets its own -n, leaving it alone")
        return []
    xdist = xdist_version(pytest_cmd)
    if xdist is None:
        if parallel_unset(config):
            warn(
                "  Test parallelism: pytest-xdist is not installed, so the suite "
                "runs serial - add pytest-xdist to the dev dependencies and set "
                "`test.python.parallel: auto`"
            )
        else:
            warn(
                "  Test parallelism requested but pytest-xdist is not installed - "
                "running serial; add pytest-xdist to the project's dev dependencies"
            )
        return []
    dist = _dist_args(args, xdist)
    mode = f", --dist {_DIST_MODE}" if dist else ""
    info(f"  Test parallelism: {workers} workers{mode} ({source})")
    return ["-n", str(workers), *dist]
