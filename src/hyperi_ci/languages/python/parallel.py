# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/parallel.py
# Purpose:   Decide whether, and how wide, to run pytest across xdist workers
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""pytest worker-count resolution.

A project should not have to hardcode a core count: a number that is right
on the CI runner is wrong on a developer box. When a project opts in with
``test.python.parallel``, the worker count comes from the host CPU budget
instead, and a project that already passes its own ``-n`` is left untouched.
"""

import os
from pathlib import Path

from hyperi_ci.common import info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.cpu import cpu_budget
from hyperi_ci.languages.python.pytest_args import project_args

# Past this width a pytest run pays more in per-worker collection than it wins
# in parallelism: hyperi-ci's own 2598-test suite takes 23.6s at 8 workers,
# 23.4s at 16 and 23.8s at 32. A project that genuinely wants more sets an
# explicit number.
_MAX_AUTO_WORKERS = 16

_WORKERS_ENV = "HYPERCI_TEST_WORKERS"

# Any of these in a project's own pytest arguments means it has already made
# the parallelism decision, including the decision to turn xdist off.
_OWN_PARALLEL_FLAGS = ("-n", "--numprocesses")
_XDIST_DISABLED = ("no:xdist", "no:xdist.plugin")


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
        if token == "-p" and index + 1 < len(tokens):
            if tokens[index + 1] in _XDIST_DISABLED:
                return True
        if token.startswith("-p") and token[2:] in _XDIST_DISABLED:
            return True
    return False


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


def _workers_from_env() -> int | None:
    """Read a worker count forced for this run only.

    ``HYPERCI_TEST_WORKERS`` overrides the config in both directions, so a
    thrashing host can be quietened without a config commit. A value of 0
    means serial.

    Returns:
        The requested worker count, 0 for serial, or None when unset or
        unparseable.

    """
    raw = os.environ.get(_WORKERS_ENV, "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        warn(f"{_WORKERS_ENV}: '{raw}' is not a number - ignoring it")
        return None


def requested_workers(config: CIConfig) -> int | None:
    """Resolve how many pytest workers the project has asked for.

    ``test.python.parallel`` takes ``false`` (the default, serial), ``true``
    or ``auto`` (derive from the host), or an explicit worker count.

    Args:
        config: Merged CI configuration.

    Returns:
        A worker count of at least 1, or None for a serial run.

    """
    forced = _workers_from_env()
    if forced is not None:
        return forced or None

    raw = config.get("test.python.parallel", False)
    if raw is True:
        return auto_workers()
    if raw is False or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 1 else None
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "auto"}:
            return auto_workers()
        if lowered in {"false", "off", ""}:
            return None
        try:
            return max(1, int(lowered))
        except ValueError:
            pass
    warn(
        f"test.python.parallel: unknown value {raw!r} - expected "
        f"true / false / auto / a worker count; running serial"
    )
    return None


def auto_workers() -> int:
    """Worker count derived from the host, capped at a useful width.

    Returns:
        A worker count of at least 1.

    """
    return max(1, min(cpu_budget(), _MAX_AUTO_WORKERS))


def xdist_installed(pytest_cmd: list[str]) -> bool:
    """Report whether the pytest that will run the suite has xdist loaded.

    Probes the resolved command rather than the ambient interpreter, so the
    answer is about the same pytest the tests will run under.

    Args:
        pytest_cmd: The resolved pytest invocation, without test arguments.

    Returns:
        True when pytest reports pytest-xdist among its registered plugins.

    """
    from hyperi_ci.common import run_cmd

    try:
        result = run_cmd([*pytest_cmd, "-VV"], check=False, capture=True)
    except (OSError, FileNotFoundError):
        return False
    if result.returncode != 0:
        return False
    return "pytest-xdist" in (result.stdout or "")


def parallel_args(
    config: CIConfig, args: list[str], pytest_cmd: list[str]
) -> list[str]:
    """Return the ``-n`` arguments to append, if any.

    Args:
        config: Merged CI configuration.
        args: pytest arguments hyperi-ci has assembled so far.
        pytest_cmd: The resolved pytest invocation, without test arguments.

    Returns:
        ``["-n", "<count>"]``, or an empty list to run serial.

    """
    workers = requested_workers(config)
    if workers is None:
        return []
    if project_sets_own_workers(args):
        info("  Test parallelism: project sets its own -n, leaving it alone")
        return []
    if not xdist_installed(pytest_cmd):
        warn(
            "  Test parallelism requested but pytest-xdist is not installed - "
            "running serial; add pytest-xdist to the project's dev dependencies"
        )
        return []
    info(f"  Test parallelism: {workers} workers (host CPU budget {cpu_budget()})")
    return ["-n", str(workers)]
