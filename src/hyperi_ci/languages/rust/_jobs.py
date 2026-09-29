# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/_jobs.py
# Purpose:   Cap cargo's parallel jobs by the memory the runner may use
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Cargo job count capped by memory as well as CPUs.

cargo runs one rustc per CPU by default and pays no attention to memory, so a
16-CPU runner with a 16Gi limit is OOM-killed once enough large crates compile
at once. Every Rust stage runs inside :func:`capped_cargo_jobs`, which sets
``CARGO_BUILD_JOBS`` for the stage and so reaches cargo, cargo-hack,
cargo-nextest, cargo-llvm-cov and cargo-pgo alike.

A job count the project or runner already chose is left alone.
``CARGO_BUILD_JOBS`` in the environment wins outright. ``build.rust.jobs``
set to a number wins next. A ``[build] jobs`` in any cargo config file comes
after that, and cargo would rank our environment variable above it, so it is
detected here and nothing is set.
"""

import os
import tomllib
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path

from hyperi_ci.common import info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.cpu import cpu_budget
from hyperi_ci.memory import GIB, MemoryLimit, memory_limit

JOBS_ENV = "CARGO_BUILD_JOBS"

# Last resort only: defaults.yaml ships `build.rust.memory_per_job_gib`.
_FALLBACK_GIB_PER_JOB = 2.0

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
    """Yield every cargo config file cargo would read, as cargo discovers them.

    Args:
        start: The directory cargo runs in.
        cargo_home: ``$CARGO_HOME``.

    Yields:
        Each existing config file.

    """
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
    """Return ``build.rust.jobs`` when it names a job count.

    Args:
        config: Merged CI configuration.

    Returns:
        The count, or None for ``auto`` or an unusable value.

    """
    value = config.get("build.rust.jobs", "auto")
    if value == "auto":
        return None
    # bool is an int subclass, and HYPERCI_* parses "1" as True.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    warn(f"  build.rust.jobs: {value!r} is not auto or a count of 1 or more, ignored")
    return None


def _gib_per_job(config: CIConfig) -> float:
    """Return ``build.rust.memory_per_job_gib``, or the fallback when unusable.

    Args:
        config: Merged CI configuration.

    Returns:
        A positive GiB figure.

    """
    value = config.get("build.rust.memory_per_job_gib", _FALLBACK_GIB_PER_JOB)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    warn(
        f"  build.rust.memory_per_job_gib: {value!r} is not a positive number, "
        f"using {_FALLBACK_GIB_PER_JOB}"
    )
    return _FALLBACK_GIB_PER_JOB


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
