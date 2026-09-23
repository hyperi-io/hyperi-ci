#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/sweep-fleet.py
# Purpose:   Run the whole ci-test-* fleet against hyperi-ci@main and read it
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Dispatch every fixture, watch it, and fail on what it could not prove.

A merge to main changes what every consumer runs, immediately, because the
workflows float `@main`. Until this existed nothing re-ran the fleet against
the new main, so the first evidence was a consumer going red.

A bare `workflow_dispatch` on a fixture is a validate-only run: the gate action
sets run-checks and run-build true and will-publish false, so quality, test and
build execute and nothing is tagged or published.

The two ways a sweep lies are the two it hard-fails on. A sweep that ran
NOTHING is not a pass -- an empty fleet, a filter that matched nobody, or every
dispatch refused all look like silence. And a repo that could not be reached is
not a repo that is fine.

Usage:
    uv run scripts/sweep-fleet.py
    uv run scripts/sweep-fleet.py --only ci-test-go-app --timeout-minutes 30
    uv run scripts/sweep-fleet.py --language rust --dry-run
Exit 1 on a failing fixture, 2 when the sweep proved nothing.
"""

import argparse
import importlib.util
import json
import subprocess
import sys
import time
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

PASS = "pass"
FAIL = "fail"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"

_WORKFLOW = "ci.yml"
_POLL_SECONDS = 20


@dataclass(frozen=True, slots=True)
class Result:
    """What one fixture's sweep run concluded."""

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


def select_targets(
    fleet: list[fixture_fleet.Entry], only: list[str], language: str
) -> list[fixture_fleet.Entry]:
    """Fixtures this sweep covers, in name order.

    Args:
        fleet: Fleet entries.
        only: Explicit fixture names. Empty means the whole fleet.
        language: Restrict to one language. Empty means every language.

    Returns:
        The selected entries. An empty result is the caller's problem to
        refuse, not something to paper over here.
    """
    chosen = [
        entry
        for entry in fleet
        if (not only or entry["name"] in only)
        and (not language or entry.get("language") == language)
    ]
    return sorted(chosen, key=lambda entry: entry["name"])


def _run_ids(repo: str) -> set[int] | None:
    """Recent run ids on the fixture's CI workflow, or None when unreadable."""
    listed = _run(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--workflow",
            _WORKFLOW,
            "--limit",
            "20",
            "--json",
            "databaseId",
        ]
    )
    if listed.returncode != 0:
        return None
    try:
        return {int(run["databaseId"]) for run in json.loads(listed.stdout or "[]")}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _dispatch(repo: str) -> str:
    """Fire a validate-only run. Empty string on success, else the reason."""
    result = _run(["gh", "workflow", "run", _WORKFLOW, "-R", repo, "--ref", "main"])
    if result.returncode != 0:
        stderr = result.stderr.strip().splitlines()
        return stderr[-1] if stderr else f"gh exited {result.returncode}"
    return ""


def _await_new_run(repo: str, before: set[int], deadline: float) -> int | None:
    """The run id the dispatch created, or None if it never appeared."""
    while time.time() < deadline:
        now = _run_ids(repo)
        if now is not None and (fresh := now - before):
            return max(fresh)
        time.sleep(_POLL_SECONDS)
    return None


def _run_state(repo: str, run_id: int) -> tuple[str, str] | None:
    """(status, conclusion) for a run, or None when it cannot be read."""
    viewed = _run(
        ["gh", "run", "view", str(run_id), "-R", repo, "--json", "status,conclusion"]
    )
    if viewed.returncode != 0:
        return None
    try:
        data = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return str(data.get("status", "")), str(data.get("conclusion", ""))


def _read_jobs(repo: str, run_id: int) -> list[dict] | None:
    """Every job of a finished run, or None when it cannot be read."""
    viewed = _run(["gh", "run", "view", str(run_id), "-R", repo, "--json", "jobs"])
    if viewed.returncode != 0:
        return None
    try:
        return json.loads(viewed.stdout or "{}").get("jobs", [])
    except json.JSONDecodeError:
        return None


def sweep_verdict(targets: list[str], results: list[Result]) -> tuple[int, list[str]]:
    """Turn per-fixture results into an exit code and a report.

    Args:
        targets: Fixture names the sweep set out to run.
        results: One per fixture that produced an answer.

    Returns:
        (exit code, report lines). 0 all green, 1 a fixture failed, 2 the
        sweep proved nothing -- it ran none, or could not reach one.
    """
    lines = [
        f"  {result.state:<11} {result.fixture} - {result.detail}" for result in results
    ]
    if not targets:
        return 2, ["ERROR: the sweep selected 0 fixtures - it proved nothing."]
    if not results:
        return 2, ["ERROR: the sweep ran 0 fixtures - it proved nothing."]

    unanswered = sorted(set(targets) - {result.fixture for result in results})
    if unanswered:
        lines.append(f"  NOT RUN: {', '.join(unanswered)}")
        return 2, lines
    if any(result.state == UNREACHABLE for result in results):
        return 2, lines
    if any(result.state != PASS for result in results):
        return 1, lines
    return 0, lines


def _sweep(targets: list[fixture_fleet.Entry], timeout_minutes: int) -> list[Result]:
    """Dispatch every target, wait for all of them, and read each run."""
    deadline = time.time() + timeout_minutes * 60
    pending: dict[str, int] = {}
    results: list[Result] = []

    for entry in targets:
        repo = f"{fixture_fleet.ORG}/{entry['name']}"
        before = _run_ids(repo)
        if before is None:
            results.append(Result(entry["name"], UNREACHABLE, "cannot list its runs"))
            continue
        if reason := _dispatch(repo):
            results.append(
                Result(entry["name"], UNREACHABLE, f"dispatch refused: {reason}")
            )
            continue
        run_id = _await_new_run(repo, before, min(deadline, time.time() + 180))
        if run_id is None:
            results.append(
                Result(entry["name"], UNREACHABLE, "dispatch produced no run")
            )
            continue
        pending[entry["name"]] = run_id
        print(f"  dispatched {entry['name']} -> run {run_id}")

    while pending and time.time() < deadline:
        for name, run_id in list(pending.items()):
            repo = f"{fixture_fleet.ORG}/{name}"
            state = _run_state(repo, run_id)
            if state is None:
                results.append(Result(name, UNREACHABLE, f"cannot read run {run_id}"))
                del pending[name]
                continue
            if state[0] != "completed":
                continue
            del pending[name]
            jobs = _read_jobs(repo, run_id)
            if jobs is None:
                results.append(
                    Result(name, UNREACHABLE, f"cannot read run {run_id} jobs")
                )
                continue
            passed, _ = rehearse_branch.summarise_jobs(jobs)
            results.append(Result(name, PASS if passed else FAIL, f"run {run_id}"))
        if pending:
            time.sleep(_POLL_SECONDS)

    for name, run_id in pending.items():
        results.append(Result(name, TIMEOUT, f"run {run_id} still going"))
    return results


def main() -> int:
    """Sweep the fleet and report what it proved."""
    parser = argparse.ArgumentParser(description="Full-fleet E2E sweep")
    parser.add_argument("--only", default="", help="comma-separated fixture names")
    parser.add_argument("--language", default="", help="restrict to one language")
    parser.add_argument("--timeout-minutes", type=int, default=60)
    parser.add_argument(
        "--dry-run", action="store_true", help="name the targets, dispatch nothing"
    )
    args = parser.parse_args()

    fleet = fixture_fleet.load_fleet()
    if problems := fixture_fleet.mask_problems(fleet):
        print("Masks in config/fixtures.yaml that nothing can ever remove:")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    only = [name.strip() for name in args.only.split(",") if name.strip()]
    targets = select_targets(fleet, only, args.language)
    unknown = sorted(set(only) - fixture_fleet.declared_names(fleet))
    if unknown:
        print(f"ERROR: not in config/fixtures.yaml: {', '.join(unknown)}")
        return 2

    names = [entry["name"] for entry in targets]
    print(
        f"Sweeping {len(names)} fixture(s) against hyperi-ci@main: {', '.join(names)}"
    )
    if lines := fixture_fleet.mask_lines(fleet):
        print("Declared masks - features this fleet is NOT exercising:")
        for line in lines:
            print(line)

    if args.dry_run:
        print("--dry-run: nothing dispatched")
        return 0 if names else 2

    results = _sweep(targets, args.timeout_minutes)
    code, lines = sweep_verdict(names, results)
    print("Sweep results:")
    for line in lines:
        print(line)
    if code == 0:
        print(f"FLEET GREEN: {len(results)} fixture(s) ran hyperi-ci@main and passed.")
    elif code == 1:
        print("FLEET RED: a fixture failed on current main - consumers are next.")
    else:
        print(
            "SWEEP INCONCLUSIVE: it did not prove main is safe. Read it, do not re-run blind."
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
