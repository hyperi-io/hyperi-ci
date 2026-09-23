#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/rehearse-gate.py
# Purpose:   Gate - a workflow change reaches main only once a fixture ran it
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Refuse a workflow change no fixture has run (issue #215).

Workflows and composite actions float `@main`, so a merge reaches every
consumer at once, while the CLI waits for a PyPI release. That split broke the
fleet twice in one week: a Gate job shipped with a `gate-check` subcommand the
wheel did not yet carry and left logreducer red for a day, and a composite
verify step ran `cargo-llvm-cov --version` on a cargo SUBCOMMAND, which refuses
it, and broke the Rust test leg everywhere. A green unit suite said nothing
about either.

`scripts/rehearse-branch.py` runs the candidate against a real fixture and
writes a RECORD into the fixture's rehearsal PR: the hyperi-ci commit it ran
and the fixture run that proved it. This reads that record back and holds the
PR to it.

The rehearsal itself is NOT run from here. Pushing a rewritten workflow file to
a fixture needs a GitHub App with `workflows: write`, and `HYPERCI_INSTALL_OVERRIDE`
needs `variables: write`; hypersec-ci-bot has neither, and minting a credential
is not an agent's call. So CI verifies and a developer runs, which is also why
the record names a COMMIT -- push another commit and the gate goes red again.

Usage:
    uv run scripts/rehearse-gate.py --pr 240
    uv run scripts/rehearse-gate.py --base-ref origin/main
Exit 1 when a required fixture is unproven, 2 when one cannot be read.
"""

import argparse
import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_fleet  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "rehearse_branch", Path(__file__).resolve().parent / "rehearse-branch.py"
)
assert _SPEC is not None and _SPEC.loader is not None  # a real file always resolves
rehearse_branch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rehearse_branch)

PROVEN = "proven"
UNPROVEN = "unproven"
FAILED = "failed"
UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the gate could establish about one fixture."""

    fixture: str
    state: str
    detail: str


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """subprocess.run with the repo's UTF-8 policy pinned."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def changed_paths_from_pr(pr: int) -> list[str] | None:
    """Files the PR touches, or None when GitHub cannot be asked."""
    result = _run(
        [
            "gh",
            "api",
            f"/repos/{fixture_fleet.ORG}/hyperi-ci/pulls/{pr}/files",
            "--paginate",
            "--jq",
            ".[].filename",
        ]
    )
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def changed_paths_from_git(base_ref: str) -> list[str] | None:
    """Files this branch changes against ``base_ref``, or None on a git error."""
    result = _run(["git", "diff", "--name-only", f"{base_ref}...HEAD"])
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def _fixture_prs(repo: str) -> list[dict] | None:
    """Recent pull requests on a fixture, newest activity first."""
    result = _run(
        [
            "gh",
            "api",
            f"/repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=50",
        ]
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None


def find_record(prs: list[dict], head_sha: str) -> dict[str, str] | None:
    """The rehearsal record for ``head_sha``, newest first."""
    for pull in prs:
        record = rehearse_branch.parse_record(pull.get("body"))
        if record and record["hyperi-ci-sha"] == head_sha:
            return record
    return None


def verify(fixture: str, head_sha: str) -> Outcome:
    """Establish whether ``head_sha`` was rehearsed against ``fixture``.

    The record says which run proved it; the run is then re-read job by job, so
    the verdict comes from GitHub rather than from the body somebody wrote.

    Args:
        fixture: Upstream fixture repo name.
        head_sha: The hyperi-ci commit under review.

    Returns:
        The outcome, which is UNREACHABLE whenever the answer is unknown rather
        than known-bad.
    """
    repo = f"{fixture_fleet.ORG}/{fixture}"
    prs = _fixture_prs(repo)
    if prs is None:
        return Outcome(fixture, UNREACHABLE, "could not read its pull requests")

    record = find_record(prs, head_sha)
    if record is None:
        return Outcome(fixture, UNPROVEN, f"no rehearsal record for {head_sha[:12]}")
    if record["verdict"] != "pass":
        return Outcome(fixture, FAILED, f"recorded verdict {record['verdict']}")

    viewed = _run(
        ["gh", "run", "view", record["run-id"], "-R", repo, "--json", "jobs"],
        timeout=120,
    )
    if viewed.returncode != 0:
        return Outcome(fixture, UNREACHABLE, f"could not read run {record['run-id']}")
    try:
        jobs = json.loads(viewed.stdout or "{}").get("jobs", [])
    except json.JSONDecodeError:
        return Outcome(fixture, UNREACHABLE, f"unreadable run {record['run-id']}")

    passed, _ = rehearse_branch.summarise_jobs(jobs)
    if not passed:
        return Outcome(fixture, FAILED, f"run {record['run-id']} did not pass")
    return Outcome(fixture, PROVEN, f"run {record['run-id']}")


def gate_verdict(required: list[str], outcomes: list[Outcome]) -> tuple[int, list[str]]:
    """Turn per-fixture outcomes into an exit code and a report.

    A required fixture that produced no outcome is the case this gate exists
    for. Reporting success on nothing having run is how a gate becomes a
    formality.

    Args:
        required: Fixture names the diff has to be proven against.
        outcomes: One per fixture the gate managed to ask about.

    Returns:
        (exit code, report lines). 0 proven, 1 unproven or failed, 2 unknown.
    """
    lines = [
        f"  {outcome.state:<11} {outcome.fixture} - {outcome.detail}"
        for outcome in outcomes
    ]
    if not required:
        return 0, ["No consumer surface touched - rehearsal not required."]

    seen = {outcome.fixture for outcome in outcomes}
    missing = sorted(set(required) - seen)
    if missing:
        lines.append(f"  NOT CHECKED: {', '.join(missing)}")
        return 2, lines

    if any(outcome.state == UNREACHABLE for outcome in outcomes):
        return 2, lines
    if any(outcome.state != PROVEN for outcome in outcomes):
        return 1, lines
    return 0, lines


def _advice(branch: str, unproven: list[str]) -> list[str]:
    """The command that clears the gate."""
    return [
        "",
        "Rehearse the branch against each fixture, then push nothing else:",
        *[
            f"  uv run scripts/rehearse-branch.py --branch {branch} "
            f"--repo {fixture_fleet.ORG}/{name}"
            for name in unproven
        ],
        "The record names the COMMIT, so a further push needs a further rehearsal.",
    ]


def main() -> int:
    """Hold the PR to a rehearsal on every fixture its diff can reach."""
    parser = argparse.ArgumentParser(description="Fixture rehearsal gate")
    parser.add_argument(
        "--pr", type=int, help="read changed files from this hyperi-ci PR"
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="diff against this ref when --pr is absent",
    )
    parser.add_argument("--branch", default="", help="branch name for the advice line")
    parser.add_argument("--head-sha", default="", help="commit under review")
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        help="override the diff (testing)",
    )
    args = parser.parse_args()

    if args.changed_file:
        changed = args.changed_file
    elif args.pr:
        changed = changed_paths_from_pr(args.pr)
    else:
        changed = changed_paths_from_git(args.base_ref)
    if changed is None:
        print("ERROR: could not read the changed files - the gate proved nothing.")
        return 2

    fleet = fixture_fleet.load_fleet()
    required_entries = fixture_fleet.select_for_paths(
        changed, fleet, fixture_fleet.read_workflow_texts()
    )
    required = [entry["name"] for entry in required_entries]

    if not required:
        print("No consumer surface touched - rehearsal not required.")
        return 0

    head_sha = args.head_sha or _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    branch = (
        args.branch or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    )

    print(f"Rehearsal required for {head_sha[:12]}: {', '.join(required)}")
    outcomes = [verify(name, head_sha) for name in required]
    code, lines = gate_verdict(required, outcomes)
    for line in lines:
        print(line)

    if code == 0:
        print("REHEARSED: every required fixture ran this commit and passed.")
        return 0
    unproven = [o.fixture for o in outcomes if o.state in (UNPROVEN, FAILED)]
    if unproven:
        for line in _advice(branch, unproven):
            print(line)
    print(
        "NOT REHEARSED: this change reaches every consumer through @main the "
        "moment it merges."
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
