# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/_jobs.py
# Purpose:   Cap cargo's parallel jobs by the memory the runner may use
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Cargo job count capped by memory as well as CPUs.

cargo runs one rustc per CPU regardless of memory, so a 16-CPU runner with a
16Gi limit gets OOM-killed. Setting ``CARGO_BUILD_JOBS`` reaches cargo and
every cargo subcommand tool alike.

Precedence: ``CARGO_BUILD_JOBS`` in the environment, then a numeric
``build.rust.jobs``, then a ``[build] jobs`` in any cargo config file. The
last is detected and nothing is set, since cargo ranks our env var above it.
"""

import os
import tomllib
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path

from hyperi_ci.common import info, warn
from hyperi_ci.config import CIConfig, shipped_default
from hyperi_ci.cpu import cpu_budget
from hyperi_ci.memory import GIB, MemoryLimit, memory_limit

JOBS_ENV = "CARGO_BUILD_JOBS"

_CONFIG_NAMES = ("config.toml", "config")


def jobs_for(cpus: int, limit: MemoryLimit | None, gib_per_job: float) -> int:
    """Return the job count the CPUs and the memory limit both allow.

    Args:
        cpus: The CPU budget.
        limit: The memory limit, or None when nothing reported one.
        gib_per_job: Memory to plan for each concurrent rustc.

    Returns:
        At least 1, at most ``cpus``.

    """
    jobs = max(1, cpus)
    if limit is not None:
        jobs = min(jobs, int(limit.limit_bytes // (gib_per_job * GIB)))
    return max(1, jobs)


def _config_files(start: Path, cargo_home: Path) -> Iterator[Path]:
    """Yield every existing cargo config file, in cargo's discovery order."""
    for directory in (start, *start.parents):
        for name in _CONFIG_NAMES:
            candidate = directory / ".cargo" / name
            if candidate.is_file():
                yield candidate
    for name in _CONFIG_NAMES:
        candidate = cargo_home / name
        if candidate.is_file():
            yield candidate


def config_file_setting_jobs(start: Path, cargo_home: Path) -> Path | None:
    """Return the first cargo config file that sets ``[build] jobs``.

    Args:
        start: The directory cargo runs in.
        cargo_home: ``$CARGO_HOME``.

    Returns:
        The file, or None when none sets it. An unreadable file is skipped,
        and cargo reports it when it runs.

    """
    for path in _config_files(start, cargo_home):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        build = data.get("build")
        if isinstance(build, dict) and "jobs" in build:
            return path
    return None


def _override(config: CIConfig) -> int | None:
    """Return ``build.rust.jobs`` as a count, or None for ``auto`` or a bad value."""
    value = config.setting("build.rust.jobs")
    if value == "auto":
        return None
    # bool is an int subclass, and HYPERCI_* parses "1" as True.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    warn(f"  build.rust.jobs: {value!r} is not auto or a count of 1 or more, ignored")
    return None


def _gib_per_job(config: CIConfig) -> float:
    """Return ``build.rust.memory_per_job_gib``, the shipped value when unusable."""
    value = config.setting("build.rust.memory_per_job_gib")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    shipped = float(shipped_default("build.rust.memory_per_job_gib"))
    warn(
        f"  build.rust.memory_per_job_gib: {value!r} is not a positive number, "
        f"using {shipped}"
    )
    return shipped


def cargo_jobs_env(
    config: CIConfig,
    environ: MutableMapping[str, str],
    *,
    cwd: Path | None = None,
    limit: MemoryLimit | None = None,
    cpus: int | None = None,
) -> dict[str, str]:
    """Return the ``CARGO_BUILD_JOBS`` setting a Rust stage should run with.

    Args:
        config: Merged CI configuration.
        environ: The environment the stage runs in.
        cwd: The directory cargo runs in; the process cwd if None.
        limit: The memory limit; read from the host if None.
        cpus: The CPU budget; read from the host if None.

    Returns:
        ``{"CARGO_BUILD_JOBS": "<n>"}``, or empty when the job count is
        already chosen elsewhere.

    """
    if existing := environ.get(JOBS_ENV):
        info(f"cargo jobs: {existing}, from {JOBS_ENV} in the environment")
        return {}

    if (override := _override(config)) is not None:
        info(f"cargo jobs: {override}, from build.rust.jobs")
        return {JOBS_ENV: str(override)}

    cargo_home = Path(environ.get("CARGO_HOME") or Path.home() / ".cargo")
    if found := config_file_setting_jobs(cwd or Path.cwd(), cargo_home):
        info(f"cargo jobs: from [build] jobs in {found}")
        return {}

    gib_per_job = _gib_per_job(config)
    cpus = cpus if cpus is not None else cpu_budget()
    limit = limit if limit is not None else memory_limit()
    jobs = jobs_for(cpus, limit, gib_per_job)
    memory = (
        f"{limit.gib:.1f} GiB memory limit from {limit.source}"
        if limit is not None
        else "no memory limit found"
    )
    info(
        f"cargo jobs: {jobs} ({memory}, {gib_per_job:g} GiB per job, CPU budget {cpus})"
    )
    return {JOBS_ENV: str(jobs)}


@contextmanager
def capped_cargo_jobs(
    config: CIConfig,
    environ: MutableMapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
    limit: MemoryLimit | None = None,
    cpus: int | None = None,
) -> Iterator[None]:
    """Set ``CARGO_BUILD_JOBS`` for the duration of a Rust stage.

    Args:
        config: Merged CI configuration.
        environ: The environment to set it in; ``os.environ`` if None.
        cwd: As for :func:`cargo_jobs_env`.
        limit: As for :func:`cargo_jobs_env`.
        cpus: As for :func:`cargo_jobs_env`.

    Yields:
        Nothing; the setting is removed again on exit.

    """
    target = os.environ if environ is None else environ
    updates = cargo_jobs_env(config, target, cwd=cwd, limit=limit, cpus=cpus)
    target.update(updates)
    try:
        yield
    finally:
        for key in updates:
            target.pop(key, None)
