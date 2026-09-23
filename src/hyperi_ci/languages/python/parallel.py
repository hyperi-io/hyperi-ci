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

from __future__ import annotations

import configparser
import os
import shlex
import tomllib
from pathlib import Path

from hyperi_ci.common import info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.cpu import cpu_budget

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

# Where pytest reads addopts from, and the section that holds them.
_INI_SOURCES = (
    ("pytest.ini", "pytest"),
    ("tox.ini", "pytest"),
    ("setup.cfg", "tool:pytest"),
)


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


def _addopts_from_pyproject(root: Path) -> list[str]:
    """Read ``addopts`` out of ``[tool.pytest.ini_options]``.

    Args:
        root: Directory holding the project's ``pyproject.toml``.

    Returns:
        Shell-split arguments, empty when the file or the key is absent.

    """
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    raw = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        return shlex.split(raw)
    return []


def _addopts_from_ini(root: Path) -> list[str]:
    """Read ``addopts`` out of pytest's ini-style configuration files.

    Args:
        root: Directory holding the project's configuration files.

    Returns:
        Shell-split arguments from every ini source that declares them.

    """
    found: list[str] = []
    for filename, section in _INI_SOURCES:
        path = root / filename
        if not path.is_file():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        if parser.has_option(section, "addopts"):
            found.extend(shlex.split(parser.get(section, "addopts")))
    return found


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
    base = root if root is not None else Path.cwd()
    sources = [
        args,
        _addopts_from_pyproject(base),
        _addopts_from_ini(base),
        shlex.split(os.environ.get("PYTEST_ADDOPTS", "")),
    ]
    return any(_tokens_claim_parallelism(tokens) for tokens in sources)


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
