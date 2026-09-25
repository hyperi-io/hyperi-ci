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

When hyperi-ci supplies the ``-n`` it also supplies ``--dist worksteal``, which
rebalances a suite whose test durations differ widely, unless the project has
chosen its own distribution mode.
"""

import configparser
import os
import re
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

# xdist's `-d` is shorthand for `--dist=load` and overrides any `--dist`.
_OWN_DIST_FLAGS = ("--dist", "-d")

_DIST_MODE = "worksteal"

# The first pytest-xdist release that accepts `--dist worksteal`; an older one
# rejects it as a usage error and runs nothing.
_WORKSTEAL_MIN_XDIST = (3, 2)

_XDIST_PLUGIN_LINE = re.compile(r"pytest-xdist-(\d+)\.(\d+)")

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


def _split_addopts(raw: object) -> list[str]:
    """Turn an ``addopts`` value from a TOML file into arguments.

    Args:
        raw: The value as parsed, a string or a list of strings.

    Returns:
        Shell-split arguments, empty for any other type.

    """
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        return shlex.split(raw)
    return []


def _read_toml(path: Path) -> dict:
    """Parse a TOML file, empty when it is absent or malformed.

    Args:
        path: The file to read.

    Returns:
        The parsed document.

    """
    try:
        return tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _addopts_from_toml(root: Path) -> list[str]:
    """Read ``addopts`` out of every TOML table pytest 9 takes them from.

    ``pytest.toml`` and ``.pytest.toml`` hold a ``[pytest]`` table, and
    ``pyproject.toml`` holds native ``[tool.pytest]`` or ini-style
    ``[tool.pytest.ini_options]``.

    Args:
        root: Directory holding the project's configuration files.

    Returns:
        Shell-split arguments from every table that declares them.

    """
    found: list[str] = []
    for filename in ("pytest.toml", ".pytest.toml"):
        table = _read_toml(root / filename).get("pytest", {})
        if isinstance(table, dict):
            found.extend(_split_addopts(table.get("addopts")))
    tool = _read_toml(root / "pyproject.toml").get("tool", {})
    tool_pytest = tool.get("pytest", {}) if isinstance(tool, dict) else None
    if isinstance(tool_pytest, dict):
        found.extend(_split_addopts(tool_pytest.get("addopts")))
        ini_options = tool_pytest.get("ini_options", {})
        if isinstance(ini_options, dict):
            found.extend(_split_addopts(ini_options.get("addopts")))
    return found


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


def _argument_sources(args: list[str], root: Path | None) -> list[list[str]]:
    """Collect every pytest argument list the run will see.

    Args:
        args: pytest arguments hyperi-ci has assembled so far.
        root: Project directory; defaults to the working directory.

    Returns:
        hyperi-ci's own arguments, the project's ``addopts`` from each config
        file pytest reads, and ``PYTEST_ADDOPTS``, one list per source.

    """
    base = root if root is not None else Path.cwd()
    return [
        args,
        _addopts_from_toml(base),
        _addopts_from_ini(base),
        shlex.split(os.environ.get("PYTEST_ADDOPTS", "")),
    ]


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
    return any(
        _tokens_claim_parallelism(tokens) for tokens in _argument_sources(args, root)
    )


def project_sets_own_dist(args: list[str], root: Path | None = None) -> bool:
    """Report whether the project has already chosen its xdist distribution mode.

    Reads the same sources as :func:`project_sets_own_workers`.

    Args:
        args: pytest arguments hyperi-ci has assembled so far.
        root: Project directory; defaults to the working directory.

    Returns:
        True when any of those sources sets ``--dist`` or ``-d``.

    """
    return any(_tokens_claim_dist(tokens) for tokens in _argument_sources(args, root))


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


def xdist_version(pytest_cmd: list[str]) -> tuple[int, int] | None:
    """Report which pytest-xdist the pytest that will run the suite loads.

    Probes the resolved command rather than the ambient interpreter, so the
    answer is about the same pytest the tests will run under.

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
    workers = requested_workers(config)
    if workers is None:
        return []
    if project_sets_own_workers(args):
        info("  Test parallelism: project sets its own -n, leaving it alone")
        return []
    xdist = xdist_version(pytest_cmd)
    if xdist is None:
        warn(
            "  Test parallelism requested but pytest-xdist is not installed - "
            "running serial; add pytest-xdist to the project's dev dependencies"
        )
        return []
    dist = _dist_args(args, xdist)
    mode = f", --dist {_DIST_MODE}" if dist else ""
    info(
        f"  Test parallelism: {workers} workers{mode} (host CPU budget {cpu_budget()})"
    )
    return ["-n", str(workers), *dist]
