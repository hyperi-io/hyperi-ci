# Project:   HyperI CI
# File:      src/hyperi_ci/gate_audit.py
# Purpose:   Report repos whose quality gate has not actually EXECUTED
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Report when a repo's quality gate has not actually run.

The CI gate doctrine skips quality and test on non-bumping pushes to main, and
that is correct -- a docs commit has no business compiling a Rust tree. The bug
is downstream of it: a skipped job and a passing job are indistinguishable in
the only place anyone looks. A run whose gate never executed still concludes
`success`, so the repo reports green while nothing has been verified (issue
#96).

The run-level conclusion is precisely the lie, so this reads JOB level:

    ci / Plan             success
    ci / Commit messages  success
    ci / Quality          skipped     <- green run, gate never ran
    ci / Test             skipped

Two faults are reported, both independent of how mature the repo is:

``stale``   the gate last executed longer ago than the window allows
``never``   no execution at all in the runs scanned

**A FAILING gate is deliberately not a finding.** GitHub already shows a red
repo as red, so reporting it adds nothing, and pre-GA repos are expected to be
red -- a reporter that shouts about them weekly is noise, and a noisy reporter
gets ignored, which is the very fault #96 is about. What is invisible is a gate
that never ran, and that is all this reports.

Report-only. It never writes to the repos it audits.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import yaml

# Supplies one run's jobs. Injected so the walk is exercisable without a
# network, which is how the report logic is tested.
JobsLookup = Callable[[object], list[dict]]

# Both gates hang off the same `run-checks` condition, so they go dark together.
GATE_JOBS = ("Quality", "Test")

DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_WORKFLOW = "ci.yml"

# Runs to look back through before giving up.
DEFAULT_SCAN_LIMIT = 20

# A called workflow's jobs are prefixed with the calling job -- `ci / Quality`.
# Nested calls prefix again, so the gate name is the last segment.
_CALLER_PREFIX = " / "

# A matrixed job carries its parameters -- `Test (arc-native-16cpu)`; one that
# never ran shows the unexpanded expression, `Build (${{ matrix.os_arch }})`.
_MATRIX_SUFFIX = re.compile(r"\s*\(.*\)$")

# Conclusions that answer nothing: a skipped gate, a superseded run, one still
# going.
_NO_VERDICT = frozenset({"skipped", "cancelled", None})


def gate_of(job_name: str) -> str | None:
    """Return which gate a job is, or None if it is not one.

    Strips the reusable-workflow prefix and any matrix suffix, so
    `ci / Test (arc-native-16cpu)` and a bare `Test` both resolve to `Test`.
    """
    leaf = job_name.rsplit(_CALLER_PREFIX, 1)[-1]
    leaf = _MATRIX_SUFFIX.sub("", leaf).strip()
    return leaf if leaf in GATE_JOBS else None


@dataclass
class Finding:
    """One gate that is not answering, on one repo."""

    kind: str  # stale | never
    job: str
    age_days: float | None = None
    runs_scanned: int = 0

    def describe(self) -> str:
        """Render the finding for a terminal report."""
        if self.kind == "stale":
            days = round(self.age_days or 0)
            return f"{self.job}: last executed {days} day{'' if days == 1 else 's'} ago"
        runs = self.runs_scanned
        return (
            f"{self.job}: never executed in the last {runs} "
            f"run{'' if runs == 1 else 's'} — skipped every time"
        )


@dataclass
class GateStatus:
    """When a gate last produced a verdict, and what it was."""

    job: str
    conclusion: str | None = None
    completed_at: datetime | None = None
    run_url: str | None = None
    # Runs that skipped this gate before one executed it. High on a repo that
    # lands by direct push to main.
    skipped_before: int = 0

    def age_days(self, *, now: datetime) -> float | None:
        """Days since this gate last produced a verdict."""
        if self.completed_at is None:
            return None
        return (now - self.completed_at).total_seconds() / 86400


@dataclass
class RepoReport:
    """One repo's gate health."""

    repo: str
    gates: list[GateStatus] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    runs_scanned: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when every gate has answered recently."""
        return not self.findings and self.error is None

    @property
    def audited(self) -> bool:
        """True when this repo runs the workflow at all.

        A repo with no runs is not a consumer of ours and reporting it would
        bury the real findings -- the same call `audit-callers` makes.
        """
        return self.error is None


def _gh_json(args: list[str]) -> object | None:
    """Run a gh command and decode its JSON, or None on any failure."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _parse_ts(raw: object) -> datetime | None:
    """Parse a GitHub timestamp, tolerating the trailing Z and a null."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def workflow_runs(
    full_name: str,
    *,
    workflow: str = DEFAULT_WORKFLOW,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> list[dict] | None:
    """Return the most recent completed runs of a workflow, newest first.

    None means the workflow could not be read at all -- no such workflow, or no
    access. An empty list means it exists but has never run.
    """
    data = _gh_json(
        [
            "api",
            f"repos/{full_name}/actions/workflows/{workflow}/runs"
            f"?per_page={limit}&status=completed",
        ],
    )
    if not isinstance(data, dict):
        return None
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        return None
    return [run for run in runs if isinstance(run, dict)]


def run_jobs(full_name: str, run_id: object) -> list[dict]:
    """Return the jobs of one run."""
    data = _gh_json(
        ["api", f"repos/{full_name}/actions/runs/{run_id}/jobs?per_page=100"],
    )
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def scan_runs(runs: list[dict], jobs_for: JobsLookup) -> dict[str, GateStatus]:
    """Find each gate's most recent real execution across runs, newest first.

    `jobs_for` is called with a run id and returns that run's jobs, so the walk
    is testable without a network. It stops as soon as every gate has answered,
    which is the first run on a healthy repo.
    """
    statuses = {job: GateStatus(job=job) for job in GATE_JOBS}
    pending = set(GATE_JOBS)

    for run in runs:
        if not pending:
            break
        # A matrixed gate is several jobs: it executed if any leg produced a
        # verdict, and failed if any leg failed.
        verdicts: dict[str, list[dict]] = {}
        for job in jobs_for(run.get("id")):
            name = job.get("name")
            if not isinstance(name, str):
                continue
            gate = gate_of(name)
            if gate is None or gate not in pending:
                continue
            verdicts.setdefault(gate, []).append(job)

        for gate in sorted(pending):
            found = verdicts.get(gate)
            if found is None:
                # No such job in this run: a different workflow shape, not a
                # skip.
                continue
            executed = [j for j in found if j.get("conclusion") not in _NO_VERDICT]
            if not executed:
                statuses[gate].skipped_before += 1
                continue
            failed = [j for j in executed if j.get("conclusion") != "success"]
            decisive = failed[0] if failed else executed[0]
            statuses[gate].conclusion = decisive.get("conclusion")
            statuses[gate].completed_at = max(
                (ts for j in executed if (ts := _parse_ts(j.get("completed_at")))),
                default=_parse_ts(run.get("updated_at")),
            )
            statuses[gate].run_url = run.get("html_url")
            pending.discard(gate)

    return statuses


def audit_runs(
    repo: str,
    runs: list[dict],
    jobs_for: JobsLookup,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> RepoReport:
    """Turn a run history into findings.

    Split from :func:`audit_repo` so the judgement is exercisable without a
    network: `jobs_for` supplies one run's jobs.
    """
    now = now or datetime.now(UTC)
    report = RepoReport(repo=repo, runs_scanned=len(runs))

    statuses = scan_runs(runs, jobs_for)
    report.gates = [statuses[job] for job in GATE_JOBS]

    for status in report.gates:
        age = status.age_days(now=now)
        if age is None:
            # Only a finding if the gate was present and skipped; a repo with
            # no such job has nothing to lie about.
            if status.skipped_before:
                report.findings.append(
                    Finding("never", status.job, runs_scanned=report.runs_scanned)
                )
            continue
        if age > max_age_days:
            # `status.conclusion` is deliberately not consulted: a red gate is
            # already visible, an unrun one is not.
            report.findings.append(Finding("stale", status.job, age_days=age))

    return report


def audit_repo(
    full_name: str,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    workflow: str = DEFAULT_WORKFLOW,
    limit: int = DEFAULT_SCAN_LIMIT,
    now: datetime | None = None,
) -> RepoReport:
    """Audit one repo's gate execution history."""
    runs = workflow_runs(full_name, workflow=workflow, limit=limit)
    if runs is None:
        return RepoReport(
            repo=full_name, error=f"no {workflow} workflow, or it is unreadable"
        )
    if not runs:
        return RepoReport(
            repo=full_name, error=f"{workflow} exists but has never completed a run"
        )
    return audit_runs(
        full_name,
        runs,
        lambda run_id: run_jobs(full_name, run_id),
        max_age_days=max_age_days,
        now=now,
    )


# Channels that make a dormant gate expected rather than a fault.
PRERELEASE_CHANNELS = frozenset({"spike", "alpha", "beta"})


def repo_channel(full_name: str) -> str | None:
    """Return a repo's declared `publish.channel`, or None if it declares none.

    A repo that declares no channel is treated as GA: silence must not buy an
    exemption.
    """
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{full_name}/contents/.hyperi-ci.yaml",
            "--header",
            "Accept: application/vnd.github.raw+json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    try:
        doc = yaml.safe_load(result.stdout)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    publish = doc.get("publish")
    if not isinstance(publish, dict):
        return None
    channel = publish.get("channel")
    return channel if isinstance(channel, str) else None


def is_prerelease(full_name: str) -> bool:
    """Return True when a repo declares a pre-GA channel."""
    channel = repo_channel(full_name)
    return channel is not None and channel.lower() in PRERELEASE_CHANNELS


def org_repos(org: str) -> list[str]:
    """Return every non-archived repo in the org."""
    data = _gh_json(["api", f"orgs/{org}/repos?per_page=100&type=all", "--paginate"])
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("archived"):
            continue
        full_name = entry.get("full_name")
        if isinstance(full_name, str):
            names.append(full_name)
    return names
