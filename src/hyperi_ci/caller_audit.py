# Project:   HyperI CI
# File:      src/hyperi_ci/caller_audit.py
# Purpose:   Report consumer ci.yml drift against the dispatch contract
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Report when a consumer's ci.yml falls behind the dispatch contract.

`workflow_dispatch.inputs` must be declared in the workflow that RECEIVES the
event, so a reusable workflow cannot add inputs to its caller's schema. Every
consumer declares and forwards them itself and so can fall behind (issue #88).
The gap surfaces only as an HTTP 422 when someone dispatches a release, and
four of eight Rust repos were undriveable for months.

The contract is :data:`CALLER_INPUTS`: what the CLI sends
(:data:`hyperi_ci.release.dispatch.DISPATCH_INPUTS`) plus what a person sends
from the Actions UI (:data:`UI_DISPATCH_INPUTS`). It excludes the reusable
workflow's inputs that nobody dispatches (say `rust-toolchain`).

Three ways a consumer breaks, all reported, none written:

``missing``        not declared at all -- the 422
``not-forwarded``  declared but absent from the job's `with:` block, so the
                   flag is accepted and silently ignored
``required``       declared `required: true`, which fails any dispatch that
                   does not send it (a from-head release sends no `tag`)

:data:`OPTIONAL_CALLER_INPUTS` are held to the last two only, as a caller that
omits one still dispatches every release path. Its absence is noted, never
counted as drift.

Report-only: the caller file belongs to the consumer repo.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hyperi_ci.release.dispatch import DISPATCH_INPUTS

# Inputs a person sends from the Actions UI, which `hyperi-ci init` scaffolds
# but the CLI never sends.
UI_DISPATCH_INPUTS: tuple[str, ...] = (
    "skip-optimize",
    "release-unoptimized",
    "optimize-tier",
)

CALLER_INPUTS: tuple[str, ...] = (*DISPATCH_INPUTS, *UI_DISPATCH_INPUTS)

# UI inputs that `hyperi-ci init` scaffolds and older callers lack, where the
# reusable workflow's default is what those callers already get.
OPTIONAL_CALLER_INPUTS: tuple[str, ...] = ("test-tier",)

# Optional too, but only on a rust-ci.yml caller: no other language workflow
# declares them, so another caller forwarding one breaks.
RUST_OPTIONAL_CALLER_INPUTS: tuple[str, ...] = ("bolt-optimize-args",)

# A job calling one of this project's reusable workflows is a release caller.
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
    optional_absent: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the consumer honours the whole dispatch contract."""
        return not self.findings and self.error is None


def _dispatch_inputs(doc: dict) -> dict:
    """Return `on.workflow_dispatch.inputs`, tolerating YAML's `on` -> True."""
    # PyYAML reads a bare `on:` key as True.
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

    optional = OPTIONAL_CALLER_INPUTS
    match = _REUSABLE.search(uses)
    if match and match.group("name") == "rust":
        optional = (*optional, *RUST_OPTIONAL_CALLER_INPUTS)

    for name in (*CALLER_INPUTS, *optional):
        spec = declared.get(name)
        if spec is None:
            if name in optional:
                report.optional_absent.append(name)
            else:
                report.findings.append(Finding("missing", name))
            continue
        if name not in forwarded:
            report.findings.append(Finding("not-forwarded", name))
        if isinstance(spec, dict) and spec.get("required") is True:
            report.findings.append(Finding("required", name))

    return report


def audit_local(root: Path | None = None) -> CallerReport:
    """Audit the working tree's ci.yml.

    The working tree is the file about to be committed. Fleet sweeps read the
    default branch instead (:func:`audit_repo`).
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

    A local clone parked on a fix branch would report a fix that main lacks.
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
