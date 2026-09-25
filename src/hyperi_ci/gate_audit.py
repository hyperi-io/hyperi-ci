# Project:   HyperI CI
# File:      src/hyperi_ci/gate_audit.py
# Purpose:   Report repos whose quality gate has not actually EXECUTED
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
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

Each repo's full test tier is reported beside the gates: the date of its last
successful ``Test (full)`` job, or ``never``, flagged when older than the same
window. The flag is a warning only for a repo that expects full runs -- its
caller workflow has a ``schedule`` trigger, or it sets
``test.full.required_for_release`` -- and an info line for everyone else. It is
never a finding and never changes the exit status.

Report-only. It never writes to the repos it audits.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import yaml

from hyperi_ci.common import info, warn
from hyperi_ci.gh import gh_run

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
NO_VERDICT = frozenset({"skipped", "cancelled", None})

FULL_TIER_JOB = "Test (full)"

# A matrixed full-tier job carries its runner inside or after the tier, as
# `Test (full, arc-native-16cpu)` or `Test (full) (arc-native-16cpu)`; a plain
# `Test` or `Test (core, ...)` is not it.
_FULL_TIER_LEAF = re.compile(r"^Test \(full(?:, [^()]*)?\)(?:\s*\([^()]*\))?$")

# Nightly full runs arrive as `schedule` events, which a busy repo's recent
# runs can push out of the scanned window. They never answer for the gates,
# which exist to check what merged.
_SCHEDULE_EVENT = "schedule"

# The full tier runs on a schedule, a dispatch asking for it, or a release, so
# a pull-request run never carries it and is not fetched.
_NEVER_FULL_EVENTS = frozenset({"pull_request", "pull_request_target"})

# The `.hyperi-ci.yaml` opt-in that makes a release run the full tier.
_FULL_REQUIRED_KEY = ("test", "full", "required_for_release")

# YAML spells a true in several ways and a repo may quote it.
_YAML_TRUE = frozenset({"true", "yes", "on", "1"})


def gate_of(job_name: str) -> str | None:
    """Return which gate a job is, or None if it is not one.

    Strips the reusable-workflow prefix and any matrix suffix, so
    `ci / Test (arc-native-16cpu)` and a bare `Test` both resolve to `Test`.
    """
    leaf = job_name.rsplit(_CALLER_PREFIX, 1)[-1]
    leaf = _MATRIX_SUFFIX.sub("", leaf).strip()
    return leaf if leaf in GATE_JOBS else None


def is_full_tier(job_name: str) -> bool:
    """Return True when a job is the full test tier.

    Strips the reusable-workflow prefix, so `ci / Test (full)`,
    `ci / Test (full, arc-native-16cpu)` and `ci / Test (full) (arc-native-16cpu)`
    match and `ci / Test` does not.
    """
    leaf = job_name.rsplit(_CALLER_PREFIX, 1)[-1].strip()
    return _FULL_TIER_LEAF.match(leaf) is not None


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
            f"run{'' if runs == 1 else 's'} -- skipped every time"
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
class FullTierStatus:
    """When a repo's full test tier last passed."""

    completed_at: datetime | None = None
    run_url: str | None = None
    runs_scanned: int = 0
    # Whether the repo schedules full runs or requires one to release.
    expected: bool = False

    def age_days(self, *, now: datetime) -> float | None:
        """Days since the full tier last passed, None if it never has."""
        if self.completed_at is None:
            return None
        return (now - self.completed_at).total_seconds() / 86400

    def is_stale(self, *, now: datetime, max_age_days: float) -> bool:
        """True when the last pass is older than the window, or never happened."""
        age = self.age_days(now=now)
        return age is None or age > max_age_days

    def describe(self, *, now: datetime, max_age_days: float) -> str:
        """Render the full-tier date for a terminal report."""
        age = self.age_days(now=now)
        if age is None or self.completed_at is None:
            runs = self.runs_scanned
            text = (
                f"{FULL_TIER_JOB} last passed: never "
                f"(none in {runs} run{'' if runs == 1 else 's'} scanned)"
            )
        else:
            days = int(age)
            text = (
                f"{FULL_TIER_JOB} last passed: {self.completed_at:%Y-%m-%d} "
                f"({days} day{'' if days == 1 else 's'} ago)"
            )
        if not self.is_stale(now=now, max_age_days=max_age_days):
            return text
        if self.expected:
            return text + f" -- STALE, older than {max_age_days:.0f} days"
        return text + " -- not expected: no schedule, full not required to release"


@dataclass
class RepoReport:
    """One repo's gate health."""

    repo: str
    gates: list[GateStatus] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    runs_scanned: int = 0
    error: str | None = None
    # Reported beside the findings, never among them: see the module docstring.
    full_tier: FullTierStatus | None = None

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
    result = gh_run(args, check=False)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _repo_yaml(full_name: str, path: str) -> dict | None:
    """Fetch one YAML file from a repo's default branch as a mapping.

    None when the file is missing, unreadable, or not a mapping.
    """
    result = gh_run(
        [
            "api",
            f"repos/{full_name}/contents/{path}",
            "--header",
            "Accept: application/vnd.github.raw+json",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        doc = yaml.safe_load(result.stdout)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else None


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
    event: str | None = None,
) -> list[dict] | None:
    """Return the most recent completed runs of a workflow, newest first.

    None means the workflow could not be read at all -- no such workflow, or no
    access. An empty list means it exists but has never run. `event` narrows
    the list to runs of one trigger, such as `schedule`.
    """
    query = f"?per_page={limit}&status=completed"
    if event:
        query += f"&event={event}"
    data = _gh_json(
        ["api", f"repos/{full_name}/actions/workflows/{workflow}/runs{query}"],
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
    which is the first run on a healthy repo. Scheduled runs are passed over:
    a nightly run says nothing about whether the gate checked a merge.
    """
    statuses = {job: GateStatus(job=job) for job in GATE_JOBS}
    pending = set(GATE_JOBS)

    for run in runs:
        if not pending:
            break
        if run.get("event") == _SCHEDULE_EVENT:
            continue
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
            executed = [j for j in found if j.get("conclusion") not in NO_VERDICT]
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


def _run_started(run: dict) -> datetime:
    """Return when a run started, for ordering runs merged from two lists."""
    return (
        _parse_ts(run.get("created_at"))
        or _parse_ts(run.get("updated_at"))
        or datetime.min.replace(tzinfo=UTC)
    )


def scan_full_tier(
    runs: list[dict], jobs_for: JobsLookup, *, limit: int = DEFAULT_SCAN_LIMIT
) -> FullTierStatus:
    """Find the newest run whose full test tier passed.

    `runs` may be merged from several listings: duplicates are dropped and the
    walk goes newest first. Pull-request runs are passed over unfetched, and
    the walk gives up after fetching `limit` runs. A matrixed full tier passed
    when no leg failed and at least one produced a verdict.
    """
    unique = {run.get("id"): run for run in runs}
    ordered = sorted(unique.values(), key=_run_started, reverse=True)
    status = FullTierStatus()

    for run in ordered:
        if status.runs_scanned >= limit:
            break
        if run.get("event") in _NEVER_FULL_EVENTS:
            continue
        status.runs_scanned += 1
        legs = [
            job
            for job in jobs_for(run.get("id"))
            if isinstance(job.get("name"), str) and is_full_tier(job["name"])
        ]
        executed = [j for j in legs if j.get("conclusion") not in NO_VERDICT]
        if not executed or any(j.get("conclusion") != "success" for j in executed):
            continue
        status.completed_at = max(
            (ts for j in executed if (ts := _parse_ts(j.get("completed_at")))),
            default=_parse_ts(run.get("updated_at")),
        )
        status.run_url = run.get("html_url")
        break

    return status


def _cached(jobs_for: JobsLookup) -> JobsLookup:
    """Wrap a jobs lookup so each run is fetched once across both walks."""
    seen: dict[object, list[dict]] = {}

    def lookup(run_id: object) -> list[dict]:
        if run_id not in seen:
            seen[run_id] = jobs_for(run_id)
        return seen[run_id]

    return lookup


def audit_runs(
    repo: str,
    runs: list[dict],
    jobs_for: JobsLookup,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
    scheduled_runs: list[dict] | None = None,
    full_tier_expected: bool = False,
    full_tier_limit: int = DEFAULT_SCAN_LIMIT,
) -> RepoReport:
    """Turn a run history into findings and the full tier's last pass.

    Split from :func:`audit_repo` so the judgement is exercisable without a
    network: `jobs_for` supplies one run's jobs. `scheduled_runs` are searched
    for the full tier alongside `runs`, and never for the gates.
    """
    now = now or datetime.now(UTC)
    report = RepoReport(repo=repo, runs_scanned=len(runs))
    jobs_for = _cached(jobs_for)

    statuses = scan_runs(runs, jobs_for)
    report.gates = [statuses[job] for job in GATE_JOBS]
    report.full_tier = scan_full_tier(
        [*runs, *(scheduled_runs or [])], jobs_for, limit=full_tier_limit
    )
    report.full_tier.expected = full_tier_expected

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


def log_full_tier(
    report: RepoReport, *, max_age_days: float, now: datetime | None = None
) -> None:
    """Log a repo's full-tier date, as a warning when stale and expected.

    Never touches `report.findings`, so the audit's exit status is unchanged.
    """
    if report.full_tier is None:
        return
    now = now or datetime.now(UTC)
    line = f"{report.repo}: " + report.full_tier.describe(
        now=now, max_age_days=max_age_days
    )
    stale = report.full_tier.is_stale(now=now, max_age_days=max_age_days)
    if stale and report.full_tier.expected:
        warn(line)
    else:
        info(line)


def audit_repo(
    full_name: str,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    workflow: str = DEFAULT_WORKFLOW,
    limit: int = DEFAULT_SCAN_LIMIT,
    now: datetime | None = None,
) -> RepoReport:
    """Audit one repo's gate execution history and log its full-tier date."""
    runs = workflow_runs(full_name, workflow=workflow, limit=limit)
    if runs is None:
        return RepoReport(
            repo=full_name, error=f"no {workflow} workflow, or it is unreadable"
        )
    if not runs:
        return RepoReport(
            repo=full_name, error=f"{workflow} exists but has never completed a run"
        )
    scheduled = workflow_runs(
        full_name, workflow=workflow, limit=limit, event=_SCHEDULE_EVENT
    )
    report = audit_runs(
        full_name,
        runs,
        lambda run_id: run_jobs(full_name, run_id),
        max_age_days=max_age_days,
        now=now,
        scheduled_runs=scheduled,
        full_tier_expected=expects_full_tier(full_name, workflow=workflow),
        full_tier_limit=limit,
    )
    log_full_tier(report, max_age_days=max_age_days, now=now)
    return report


def triggers_on_schedule(workflow_doc: dict) -> bool:
    """Return True when a parsed workflow declares a `schedule` trigger.

    PyYAML reads a bare `on:` key as the boolean True, so both keys are tried.
    """
    triggers = workflow_doc.get("on", workflow_doc.get(True))
    if isinstance(triggers, dict | list):
        return _SCHEDULE_EVENT in triggers
    return triggers == _SCHEDULE_EVENT


def requires_full_for_release(config: dict | None) -> bool:
    """Return True when a parsed `.hyperi-ci.yaml` sets the full-release opt-in."""
    value: object = config
    for part in _FULL_REQUIRED_KEY:
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, str):
        return value.strip().lower() in _YAML_TRUE
    return value is True


def expects_full_tier(full_name: str, *, workflow: str = DEFAULT_WORKFLOW) -> bool:
    """Return True when a repo schedules full runs or requires one to release.

    Read from the default branch. An unreadable file expects nothing, so the
    report stays an info line rather than a warning.
    """
    caller = _repo_yaml(full_name, f".github/workflows/{workflow}")
    if caller is not None and triggers_on_schedule(caller):
        return True
    return requires_full_for_release(_repo_yaml(full_name, ".hyperi-ci.yaml"))


# Channels that make a dormant gate expected rather than a fault.
PRERELEASE_CHANNELS = frozenset({"alpha", "beta"})


def repo_channel(full_name: str) -> str | None:
    """Return a repo's declared `release.channel`, or None if it declares none.

    A repo that declares no channel is treated as GA: silence must not buy an
    exemption.
    """
    doc = _repo_yaml(full_name, ".hyperi-ci.yaml")
    if doc is None:
        return None
    # Read straight off the fetched YAML, so the namespace fold that
    # `load_config` applies to a local file never runs here.
    from hyperi_ci.vocabulary import CONFIG_NAMESPACE, LEGACY_CONFIG_NAMESPACE

    for namespace in (CONFIG_NAMESPACE, LEGACY_CONFIG_NAMESPACE):
        block = doc.get(namespace)
        if not isinstance(block, dict):
            continue
        channel = block.get("channel")
        if isinstance(channel, str):
            return channel
    return None


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
