# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/runner_fit.py
# Purpose:   Record what a Rust Build used and say whether its runner fits
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Per-build runner-fit record and verdict.

Each heavy build phase marks its memory peak through
:func:`hyperi_ci.languages.rust._jobs.log_peak_memory`. :func:`finish` turns the
marks into ``runner-fit.json`` and a job summary block, with a verdict on
whether a cheaper runner profile would hold the build. The verdict only
advises. It never fails a Build and never changes a runner.
"""

import json
import os
import platform
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypeGuard

from hyperi_ci.common import append_step_summary, info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.cpu import cpu_budget, cpu_model
from hyperi_ci.memory import GIB, MemoryLimit, memory_limit

SCHEMA = 1
RECORD_FILE = "runner-fit.json"
LABEL_ENV = "HYPERCI_RUNNER_LABEL"
JOBS_ENV = "CARGO_BUILD_JOBS"

_ARCHES = ("amd64", "arm64")


@dataclass(frozen=True, slots=True)
class RunnerProfile:
    """One runner a Build can be scheduled on.

    Attributes:
        label: The ``runs-on`` label.
        arch: ``amd64`` or ``arm64``.
        cpus: CPU count.
        memory_gib: Memory in GiB.

    """

    label: str
    arch: str
    cpus: int
    memory_gib: float


@dataclass(frozen=True, slots=True)
class FitSettings:
    """The ``build.rust.runner_fit`` tunables.

    Attributes:
        min_headroom: Fraction of the memory limit left free at the peak.
        min_jobs: Concurrent rustc jobs a recommended profile must still hold.
        per_job_drift: Relative gap to ``memory_per_job_gib`` that earns a note.
        profiles: Runner profiles, cheapest first.

    """

    min_headroom: float
    min_jobs: int
    per_job_drift: float
    profiles: tuple[RunnerProfile, ...]


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether the runner fit the build, and which one would.

    Attributes:
        status: ``fits``, ``tight`` or ``unknown``.
        reason: One line saying why the status is what it is.
        headroom: Fraction of the memory limit unused at the peak, or None.
        needed_gib: Memory ``min_jobs`` concurrent jobs need, or None.
        recommended: Label of the cheapest profile that holds it, or None.
        move: True when the recommendation differs from the runner used.
        move_reason: One line behind ``move``, or why nothing was recommended.
        notes: Extra lines, such as drift from ``memory_per_job_gib``.

    """

    status: str
    reason: str
    headroom: float | None = None
    needed_gib: float | None = None
    recommended: str | None = None
    move: bool = False
    move_reason: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Phase:
    """One build phase: its name, its length and the memory peak at its end."""

    name: str
    seconds: float
    peak_bytes: int | None


@dataclass(slots=True)
class _Recorder:
    """Marks for one build run."""

    last: float
    started: float
    phases: list[Phase] = field(default_factory=list)
    optimization: str | None = None


# One Build runs per process, and begin() resets this at its start.
_recorder: _Recorder | None = None


def begin(*, now: float | None = None) -> None:
    """Start recording a build, discarding any earlier marks."""
    global _recorder
    start = time.monotonic() if now is None else now
    _recorder = _Recorder(last=start, started=start)


def record_phase(
    name: str, peak_bytes: int | None, *, now: float | None = None
) -> None:
    """Mark a finished phase. Does nothing outside a recorded build."""
    if _recorder is None:
        return
    moment = time.monotonic() if now is None else now
    _recorder.phases.append(Phase(name, moment - _recorder.last, peak_bytes))
    _recorder.last = moment


def set_optimization(label: str) -> None:
    """Name the optimisation tier the build runs, for the record."""
    if _recorder is not None:
        _recorder.optimization = label


def normalise_arch(machine: str) -> str:
    """Return ``amd64`` or ``arm64`` for a ``platform.machine()`` value."""
    lowered = machine.lower()
    if lowered in ("x86_64", "amd64"):
        return "amd64"
    if lowered in ("aarch64", "arm64"):
        return "arm64"
    return lowered


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _positive(value: object) -> float | None:
    return float(value) if _number(value) and value > 0 else None


def _profile(raw: object) -> RunnerProfile | None:
    if not isinstance(raw, Mapping):
        return None
    label, arch = raw.get("label"), raw.get("arch")
    cpus, memory = raw.get("cpus"), _positive(raw.get("memory_gib"))
    if isinstance(label, str) and label and arch in _ARCHES and _count(cpus) and memory:
        return RunnerProfile(label, str(arch), cpus, memory)
    return None


def parse_settings(raw: object) -> FitSettings | None:
    """Return the tunables from ``build.rust.runner_fit``, or None when unusable.

    A malformed block warns and returns None, so the verdict is ``unknown``
    and the Build carries on.
    """
    problem = "is not a mapping"
    if isinstance(raw, Mapping):
        headroom, jobs = raw.get("min_headroom"), raw.get("min_jobs")
        drift, listed = raw.get("per_job_drift"), raw.get("profiles")
        profiles = (
            [_profile(item) for item in listed] if isinstance(listed, list) else []
        )
        usable = tuple(p for p in profiles if p is not None)
        if not (_number(headroom) and 0 <= headroom < 1):
            problem = f"min_headroom {headroom!r} is not a number from 0 to under 1"
        elif not _count(jobs):
            problem = f"min_jobs {jobs!r} is not a count of 1 or more"
        elif not (_number(drift) and drift >= 0):
            problem = f"per_job_drift {drift!r} is not a number of 0 or more"
        elif not usable or len(usable) != len(profiles):
            problem = "profiles needs a list of {label, arch, cpus, memory_gib}"
        else:
            return FitSettings(float(headroom), jobs, float(drift), usable)
    warn(f"  build.rust.runner_fit: {problem}, no runner verdict")
    return None


def _against(current: str | None, chosen: RunnerProfile, settings: FitSettings) -> str:
    """Say how the recommended profile compares with the runner used."""
    labels = [p.label for p in settings.profiles]
    if current is None:
        return "the runner label is unknown"
    if current not in labels:
        return f"{current} is not in build.rust.runner_fit.profiles"
    if labels.index(current) > labels.index(chosen.label):
        return f"cheaper than {current}"
    return f"{current} is too small for it"


def verdict(
    *,
    arch: str,
    runner_label: str | None,
    per_job_peak_gib: float | None,
    max_peak_gib: float | None,
    limit_gib: float | None,
    memory_per_job_gib: float | None,
    settings: FitSettings | None,
) -> Verdict:
    """Judge whether the runner fit the build and which profile would.

    Args:
        arch: ``amd64`` or ``arm64``.
        runner_label: The ``runs-on`` label the build ran on, or None.
        per_job_peak_gib: Peak memory divided by cargo jobs, or None.
        max_peak_gib: Highest memory peak across the phases, or None.
        limit_gib: The memory limit the build ran under, or None.
        memory_per_job_gib: The configured per-job plan, or None.
        settings: The tunables, or None when the block was malformed.

    Returns:
        The verdict. ``unknown`` names what is missing and still carries a
        recommendation when the per-job peak is known. A recommendation of
        None means no profile holds the build.

    """
    if settings is None:
        return Verdict("unknown", "build.rust.runner_fit is unusable")

    missing = []
    if max_peak_gib is None:
        missing.append("no memory peak recorded")
    if limit_gib is None or limit_gib <= 0:
        missing.append("no memory limit found")

    headroom = None
    if max_peak_gib is not None and limit_gib is not None and limit_gib > 0:
        headroom = 1 - max_peak_gib / limit_gib
        status = "tight" if headroom < settings.min_headroom else "fits"
        reason = (
            f"peak {max_peak_gib:.1f} GiB of {limit_gib:.1f} GiB leaves "
            f"{headroom:.0%} free, floor {settings.min_headroom:.0%}"
        )
    else:
        status = "unknown"
        reason = ", ".join(missing)

    if per_job_peak_gib is None:
        return Verdict(status, f"{reason}, no per-job peak", headroom=headroom)

    needed = per_job_peak_gib * settings.min_jobs
    chosen = next(
        (
            p
            for p in settings.profiles
            if p.arch == arch and p.memory_gib * (1 - settings.min_headroom) >= needed
        ),
        None,
    )
    notes = []
    if memory_per_job_gib and (
        abs(per_job_peak_gib - memory_per_job_gib) / memory_per_job_gib
        > settings.per_job_drift
    ):
        notes.append(
            f"per-job peak {per_job_peak_gib:.1f} GiB against "
            f"build.rust.memory_per_job_gib {memory_per_job_gib:g}"
        )
    if chosen is None:
        return Verdict(
            status,
            reason,
            headroom=headroom,
            needed_gib=needed,
            move_reason=(
                f"no {arch} profile holds {needed:.1f} GiB "
                f"at {settings.min_headroom:.0%} headroom"
            ),
            notes=tuple(notes),
        )
    move = chosen.label != runner_label
    move_reason = ""
    if move:
        move_reason = (
            f"per-job peak {per_job_peak_gib:.1f} GiB x {settings.min_jobs} jobs "
            f"fits {chosen.label} ({chosen.memory_gib:g} GiB), "
            f"{_against(runner_label, chosen, settings)}"
        )
    return Verdict(
        status,
        reason,
        headroom=headroom,
        needed_gib=needed,
        recommended=chosen.label,
        move=move,
        move_reason=move_reason,
        notes=tuple(notes),
    )


def _gib(value: int | None) -> float | None:
    return None if value is None else value / GIB


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _jobs(environ: Mapping[str, str]) -> int | None:
    raw = environ.get(JOBS_ENV, "")
    return int(raw) if raw.isdigit() and int(raw) >= 1 else None


def build_record(
    config: CIConfig,
    phases: Sequence[Phase],
    *,
    total_seconds: float,
    exit_code: int,
    optimization: str | None,
    environ: Mapping[str, str],
    limit: MemoryLimit | None,
    cpus: int,
    cpu_name: str | None,
    machine: str,
) -> dict[str, Any]:
    """Return the ``runner-fit.json`` content for a finished build.

    Args:
        config: Merged CI configuration.
        phases: The marks the build recorded.
        total_seconds: Seconds from the start of the build to now.
        exit_code: The build's exit code.
        optimization: ``pgo+bolt``, ``pgo``, ``bolt``, ``none`` or None.
        environ: The process environment.
        limit: The memory limit, or None when nothing reported one.
        cpus: The CPU budget.
        cpu_name: The CPU model, or None.
        machine: ``platform.machine()``.

    Returns:
        A JSON-ready mapping with ``schema`` 1.

    """
    arch = normalise_arch(machine)
    label = environ.get(LABEL_ENV) or environ.get("RUNNER_NAME") or None
    jobs = _jobs(environ)
    peaks = [p.peak_bytes for p in phases if p.peak_bytes is not None]
    max_peak = _gib(max(peaks)) if peaks else None
    per_job = max_peak / jobs if max_peak is not None and jobs else None
    planned = _positive(config.setting("build.rust.memory_per_job_gib"))
    judged = verdict(
        arch=arch,
        runner_label=label,
        per_job_peak_gib=per_job,
        max_peak_gib=max_peak,
        limit_gib=limit.gib if limit is not None else None,
        memory_per_job_gib=planned,
        settings=parse_settings(config.setting("build.rust.runner_fit")),
    )
    return {
        "schema": SCHEMA,
        "repo": environ.get("GITHUB_REPOSITORY") or None,
        "run_id": environ.get("GITHUB_RUN_ID") or None,
        "run_attempt": environ.get("GITHUB_RUN_ATTEMPT") or None,
        "arch": arch,
        "runner_label": label,
        "cpu_model": cpu_name,
        "cpu_budget": cpus,
        "memory_limit_gib": _rounded(limit.gib if limit else None, 2),
        "memory_limit_source": limit.source if limit else None,
        "cargo_jobs": jobs,
        "memory_per_job_gib": planned,
        "optimization": optimization,
        "exit_code": exit_code,
        "phases": [
            {
                "name": p.name,
                "seconds": round(p.seconds, 1),
                "peak_gib": _rounded(_gib(p.peak_bytes), 2),
            }
            for p in phases
        ],
        "total_seconds": round(total_seconds, 1),
        "max_peak_gib": _rounded(max_peak, 2),
        "per_job_peak_gib": _rounded(per_job, 2),
        "verdict": {
            **asdict(judged),
            "headroom": _rounded(judged.headroom, 3),
            "needed_gib": _rounded(judged.needed_gib, 2),
            "notes": list(judged.notes),
        },
    }


def summary_markdown(record: Mapping[str, Any]) -> str:
    """Render a record as a job summary block: a phase table and one verdict line."""
    rows = ["| Phase | Seconds | Peak GiB |", "| --- | --- | --- |"]
    for phase in record["phases"]:
        peak = "n/a" if phase["peak_gib"] is None else f"{phase['peak_gib']:.1f}"
        rows.append(f"| {phase['name']} | {phase['seconds']:.0f} | {peak} |")
    judged = record["verdict"]
    line = f"Runner fit ({record['arch']}, {record['runner_label'] or 'unknown'}): "
    line += f"{judged['status']}, {judged['reason']}"
    if judged["move"]:
        line += f". Move to {judged['recommended']}: {judged['move_reason']}"
    elif judged["move_reason"]:
        line += f". {judged['move_reason']}"
    for note in judged["notes"]:
        line += f". {note}"
    return "### Runner fit\n\n" + "\n".join(rows) + f"\n\n{line}\n\n"


def finish(
    config: CIConfig,
    exit_code: int,
    *,
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    limit: MemoryLimit | None = None,
    cpus: int | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Write the record and the summary block for the build that just ended.

    Only a build whose workflow names its runner (``HYPERCI_RUNNER_LABEL``)
    writes anything, so a local build or a unit test leaves no file behind. A
    failure here warns and never changes the build's result.

    Args:
        config: Merged CI configuration.
        exit_code: The build's exit code.
        root: Where ``runner-fit.json`` goes, the current directory if None.
        environ: The environment, ``os.environ`` if None.
        limit: The memory limit, read from the host if None.
        cpus: The CPU budget, read from the host if None.
        now: The clock reading, for tests.

    Returns:
        The record, or None when nothing was written.

    """
    global _recorder
    recorder, _recorder = _recorder, None
    env = os.environ if environ is None else environ
    if recorder is None or not env.get(LABEL_ENV):
        return None
    moment = time.monotonic() if now is None else now
    try:
        record = build_record(
            config,
            recorder.phases,
            total_seconds=moment - recorder.started,
            exit_code=exit_code,
            optimization=recorder.optimization,
            environ=env,
            limit=limit if limit is not None else memory_limit(),
            cpus=cpus if cpus is not None else cpu_budget(),
            cpu_name=cpu_model(),
            machine=platform.machine(),
        )
        target = (root or Path.cwd()) / RECORD_FILE
        target.write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        warn(f"  could not write {RECORD_FILE}: {exc}")
        return None
    info(f"runner fit: {record['verdict']['status']}, {record['verdict']['reason']}")
    append_step_summary(summary_markdown(record))
    return record
