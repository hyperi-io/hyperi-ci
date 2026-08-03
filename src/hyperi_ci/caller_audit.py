# Project:   HyperI CI
# File:      src/hyperi_ci/caller_audit.py
# Purpose:   Report consumer ci.yml drift against the dispatch contract
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Report when a consumer's ci.yml falls behind the dispatch contract.

`workflow_dispatch.inputs` must be declared in the workflow that RECEIVES the
event, so a reusable workflow cannot add inputs to its caller's schema. Every
consumer declares and forwards them itself, and every consumer can therefore
fall behind (issue #88).

Nothing reports the gap on its own. It surfaces as an HTTP 422 at dispatch
time, and only when someone tries to release that way -- four of eight Rust
repos were undriveable for months before anyone needed the path.

The contract is :data:`hyperi_ci.publish.dispatch.DISPATCH_INPUTS`, which is
what the CLI actually sends, rather than the reusable workflow's full
`workflow_call.inputs`. A `workflow_call` input nobody dispatches (say
`rust-toolchain`) has no business in a consumer's dispatch schema.

Three ways a consumer breaks, all reported, none written:

``missing``        not declared at all -- the 422
``not-forwarded``  declared but absent from the job's `with:` block, so the
                   flag is accepted and silently ignored
``required``       declared `required: true`, which fails any dispatch that
                   does not send it (a from-head release sends no `tag`)

Reads are strictly report-only: the caller file belongs to the consumer repo.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hyperi_ci.publish.dispatch import DISPATCH_INPUTS

# Reusable workflows this project publishes. A job calling one of these is a
# release caller and is held to the dispatch contract.
_REUSABLE = re.compile(
    r"hyperi-io/hyperi-ci/\.github/workflows/(?P<name>[a-z-]+)-ci\.yml@",
)

# `${{ inputs.foo }}` / `${{ inputs.foo || '' }}` in a `with:` value.
_FORWARDED = re.compile(r"inputs\.([A-Za-z0-9_-]+)")

DEFAULT_CALLER = Path(".github/workflows/ci.yml")


@dataclass
class Finding:
    """One drifted input on one consumer."""

    kind: str  # missing | not-forwarded | required
    input_name: str

    def describe(self) -> str:
        """Render the finding for a terminal report."""
        if self.kind == "missing":
            return f"{self.input_name}: not declared in workflow_dispatch.inputs"
        if self.kind == "not-forwarded":
            return f"{self.input_name}: declared but not passed in `with:`"
        return (
            f"{self.input_name}: declared `required: true`, breaking other dispatches"
        )


@dataclass
class CallerReport:
    """What one consumer's ci.yml declares, and where it drifts."""

    repo: str
    calls: str | None = None
    declared: list[str] = field(default_factory=list)
    forwarded: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the consumer honours the whole dispatch contract."""
        return not self.findings and self.error is None


def _dispatch_inputs(doc: dict) -> dict:
    """Return `on.workflow_dispatch.inputs`, tolerating YAML's `on` -> True."""
    # PyYAML resolves a bare `on:` key to the boolean True, so a workflow
    # parsed with safe_load has its triggers under True rather than "on".
    triggers = doc.get("on")
    if triggers is None:
        triggers = doc.get(True)
    if not isinstance(triggers, dict):
        return {}
    dispatch = triggers.get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return {}
    inputs = dispatch.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _calling_job(doc: dict) -> tuple[str, dict] | None:
    """Return the (reusable workflow ref, job) that calls a hyperi-ci workflow."""
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return None
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if isinstance(uses, str) and _REUSABLE.search(uses):
            return uses, job
    return None


def audit_text(repo: str, text: str) -> CallerReport:
    """Audit one consumer's ci.yml contents against the dispatch contract."""
    report = CallerReport(repo=repo)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        report.error = f"unparseable ci.yml: {exc}"
        return report
    if not isinstance(doc, dict):
        report.error = "ci.yml is not a mapping"
        return report

    called = _calling_job(doc)
    if called is None:
        report.error = "no job calls a hyperi-ci reusable workflow"
        return report
    uses, job = called
    report.calls = uses

    declared = _dispatch_inputs(doc)
    report.declared = sorted(declared)

    with_block = job.get("with")
    with_block = with_block if isinstance(with_block, dict) else {}
    forwarded: set[str] = set()
    for value in with_block.values():
        forwarded.update(_FORWARDED.findall(str(value)))
    report.forwarded = sorted(forwarded)

    for name in DISPATCH_INPUTS:
        spec = declared.get(name)
        if spec is None:
            report.findings.append(Finding("missing", name))
            continue
        if name not in forwarded:
            report.findings.append(Finding("not-forwarded", name))
        if isinstance(spec, dict) and spec.get("required") is True:
            report.findings.append(Finding("required", name))

    return report


def audit_local(root: Path | None = None) -> CallerReport:
    """Audit the working tree's ci.yml.

    The working tree is the right source here -- it is the file about to be
    committed. Fleet sweeps read the default branch instead (see
    :func:`audit_repo`), because a checkout sitting on a fix branch reports
    drift that main still has.
    """
    root = root or Path.cwd()
    path = root / DEFAULT_CALLER
    name = root.name
    if not path.is_file():
        return CallerReport(repo=name, error=f"no {DEFAULT_CALLER}")
    return audit_text(name, path.read_text(encoding="utf-8"))


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


def audit_repo(full_name: str) -> CallerReport:
    """Audit one repo's ci.yml as it stands on the DEFAULT BRANCH.

    Reading the default branch rather than a local clone is the point: a
    working tree parked on a release branch reports a fix that main has not
    got.
    """
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{full_name}/contents/.github/workflows/ci.yml",
            "--header",
            "Accept: application/vnd.github.raw+json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return CallerReport(repo=full_name, error="no .github/workflows/ci.yml")
    return audit_text(full_name, result.stdout)


def org_repos(org: str) -> list[str]:
    """Return every non-archived repo in the org."""
    data = _gh_json(
        ["api", f"orgs/{org}/repos?per_page=100&type=all", "--paginate"],
    )
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
