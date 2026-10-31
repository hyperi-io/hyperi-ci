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

A fixture carrying a rehearse/* branch is held by a rehearsal, whose install
override points every run there at the rehearsed branch's CLI rather than
main's. The sweep waits for the branch to go and reports the fixture held if
it never does.

Before dispatching anything it waits for any unfinished hyperi-infra
runner-image canary, which dispatches the same fixtures and would have its
runs cancelled by the sweep's. A canary still running at the deadline fails
the sweep with nothing dispatched.

A run still going at the deadline proves nothing either way, so it is
inconclusive rather than red. The GitHub App token the sweep reads runs with
lives 60 minutes, so a run that outlasts one window is handed to a second
invocation under a fresh token: `--pending-file` has the first write the runs
still going, and `--collect` has the second read them to a verdict.

Usage:
    uv run scripts/sweep-fleet.py
    uv run scripts/sweep-fleet.py --only ci-test-go-app --timeout-minutes 30
    uv run scripts/sweep-fleet.py --language rust --dry-run
    uv run scripts/sweep-fleet.py --pending-file pending.json
    uv run scripts/sweep-fleet.py --collect pending.json
Exit 1 on a failing fixture, 2 when the sweep proved nothing. With
`--pending-file`, exit 0 when only runs still going are left, because the
`--collect` run decides.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from hyperi_ci.common import set_github_output

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
# A fixture takes its workflows from `@main` on merge and its CLI from PyPI on
# release, so a case for a merged-but-unreleased gate fails for a reason that
# says nothing about the gate (issue #248).
PENDING_RELEASE = "pending-release"
# A rehearse/* branch stayed on the fixture until the deadline, so it was never
# dispatched.
HELD = "held"

# Answered nothing, as distinct from answered badly - neither is PASS, so a run
# carrying one can never go green. A run still going at the deadline has not
# failed, so TIMEOUT sits here.
INCONCLUSIVE = (TIMEOUT, UNREACHABLE, PENDING_RELEASE, HELD)

_WORKFLOW = "ci.yml"
_POLL_SECONDS = 20
# Failed reads of one run, back to back, before it counts as unreachable.
_READ_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class Result:
    """What one fixture's sweep run concluded."""

    fixture: str
    state: str
    detail: str
    # Set on a TIMEOUT, so a later invocation can read the same run again.
    run_id: int | None = None


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


class HoldUnreadableError(RuntimeError):
    """Whether a rehearsal holds a fixture could not be read."""


def _rehearsal_branches(repo: str) -> list[str]:
    """The rehearsal branches on a fixture. Any at all means a rehearsal holds it.

    A rehearsal pushes its branch before it points the fixture's CLI at the
    rehearsed one, and deletes it only after putting the CLI back, so the
    branch covers the whole hold. It is read from the branches rather than the
    install override because the fleet's App token cannot read variables.

    Args:
        repo: The fixture, ``owner/name``.

    Returns:
        The branch names, empty when no rehearsal holds the fixture.

    Raises:
        HoldUnreadableError: gh failed or answered with something unreadable.
            Read as "not held", the sweep would dispatch into a rehearsal.
    """
    prefix = rehearse_branch.REHEARSAL_PREFIX
    result = _run(["gh", "api", f"repos/{repo}/git/matching-refs/heads/{prefix}"])
    if result.returncode != 0:
        stderr = result.stderr.strip().splitlines()
        detail = stderr[-1] if stderr else f"gh exited {result.returncode}"
        raise HoldUnreadableError(f"cannot list {prefix}* branches on {repo}: {detail}")
    try:
        refs = json.loads(result.stdout or "[]")
        return [str(ref["ref"]).removeprefix("refs/heads/") for ref in refs]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HoldUnreadableError(
            f"unreadable {prefix}* branch list on {repo}: {exc}"
        ) from exc


CANARY_CLEAR = "clear"
CANARY_RUNNING = "running"
CANARY_UNREAD = "unread"
# A token that can read hyperi-infra's runs. The fleet token is scoped to the
# fixtures, so without this the canary reads as unreadable in CI.
CANARY_TOKEN_ENV = "CANARY_GH_TOKEN"
_CANARY_POLL_SECONDS = 60


def wait_for_image_canary(timeout_minutes: int, token: str | None) -> str:
    """Hold the sweep while hyperi-infra's runner-image canary has a run unfinished.

    The canary dispatches the same fixtures on main, and the fixtures' CI
    concurrency group cancels whichever run started first, so a sweep
    dispatched mid-canary cancels the canary's runs (issue #608).

    Args:
        timeout_minutes: How long to wait for the canary to finish.
        token: A token that can read hyperi-infra's runs. None uses gh's own auth.

    Returns:
        CANARY_CLEAR once no canary run is unfinished, CANARY_RUNNING when one
        still is at the deadline, CANARY_UNREAD when the first read failed. A
        read failing after a run was seen keeps waiting, since that run is the
        last thing known to be going.
    """
    repo, workflow = rehearse_branch.IMAGE_CANARY
    deadline = time.time() + timeout_minutes * 60
    seen: int | None = None
    while True:
        try:
            run_id = rehearse_branch.unfinished_run(repo, workflow, token)
        except rehearse_branch.SweepUnreadableError as exc:
            if seen is None:
                print(
                    f"::warning::cannot read {repo} runs, so the sweep is NOT "
                    f"waiting for the runner-image canary and may cancel it: {exc}",
                    flush=True,
                )
                return CANARY_UNREAD
            run_id = seen
        if run_id is None:
            return CANARY_CLEAR
        if time.time() >= deadline:
            return CANARY_RUNNING
        if run_id != seen:
            print(
                f"  {workflow} run {run_id} on {repo} has not finished - "
                "waiting before dispatching anything",
                flush=True,
            )
            seen = run_id
        time.sleep(_CANARY_POLL_SECONDS)


def _start(name: str, deadline: float) -> Result | int:
    """Dispatch one fixture: the run it started, or a Result saying why none did."""
    repo = f"{fixture_fleet.ORG}/{name}"
    before = _run_ids(repo)
    if before is None:
        return Result(name, UNREACHABLE, "cannot list its runs")
    if reason := _dispatch(repo):
        return Result(name, UNREACHABLE, f"dispatch refused: {reason}")
    run_id = _await_new_run(repo, before, min(deadline, time.time() + 180))
    if run_id is None:
        return Result(name, UNREACHABLE, "dispatch produced no run")
    print(f"  dispatched {name} -> run {run_id}")
    return run_id


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
    # `or ""`: an unfinished run carries a null conclusion, which str() would
    # turn into the word "None".
    return str(data.get("status") or ""), str(data.get("conclusion") or "")


def _read_jobs(repo: str, run_id: int) -> list[dict] | None:
    """Every job of a finished run, or None when it cannot be read."""
    viewed = _run(["gh", "run", "view", str(run_id), "-R", repo, "--json", "jobs"])
    if viewed.returncode != 0:
        return None
    try:
        return json.loads(viewed.stdout or "{}").get("jobs", [])
    except json.JSONDecodeError:
        return None


def run_result(
    fixture: str, run_id: int, conclusion: str, jobs: list[dict] | None
) -> Result:
    """The verdict on one finished fixture run.

    The run's own conclusion decides it. The jobs come from a second read that
    can disagree with it, and a job-by-job verdict called a green run red
    (issue #401). Here they only name what failed.

    Args:
        fixture: The fixture the run belongs to.
        run_id: The run.
        conclusion: The run's conclusion.
        jobs: Its jobs, or None when they could not be read. Only a run that
            did not pass needs them.

    Returns:
        PASS on a ``success`` conclusion, else FAIL naming the jobs that did
        not pass.
    """
    if rehearse_branch.run_passed(conclusion):
        return Result(fixture, PASS, f"run {run_id}")
    detail = f"run {run_id} concluded {conclusion or 'nothing'}"
    if jobs is None:
        return Result(fixture, FAIL, f"{detail}; its jobs could not be read")
    failed = [
        f"{job.get('name')} {job.get('conclusion') or 'pending'}"
        for job in jobs
        if job.get("conclusion") not in rehearse_branch.PASSING_CONCLUSIONS
    ]
    if failed:
        detail += f": {', '.join(failed)}"
    return Result(fixture, FAIL, detail)


def _read_pending(pending: dict[str, int], misses: dict[str, int]) -> list[Result]:
    """Read each run still going once, and settle the ones that finished.

    Args:
        pending: Fixture -> run id. Settled and unreadable runs are removed.
        misses: Consecutive failed reads per fixture, so one gh error is not a
            verdict. Updated in place.

    Returns:
        A Result for each run settled by this pass.
    """
    settled: list[Result] = []
    for name, run_id in list(pending.items()):
        repo = f"{fixture_fleet.ORG}/{name}"
        state = _run_state(repo, run_id)
        if state is None:
            misses[name] = misses.get(name, 0) + 1
            if misses[name] >= _READ_ATTEMPTS:
                settled.append(
                    Result(
                        name,
                        UNREACHABLE,
                        f"cannot read run {run_id} ({misses[name]} reads in a row)",
                    )
                )
                del pending[name]
            continue
        misses.pop(name, None)
        status, conclusion = state
        # Finished means a conclusion to read, not only a completed status.
        if status != "completed" or not conclusion:
            continue
        del pending[name]
        jobs = (
            None if rehearse_branch.run_passed(conclusion) else _read_jobs(repo, run_id)
        )
        settled.append(run_result(name, run_id, conclusion, jobs))
    return settled


def _still_going(pending: dict[str, int]) -> list[Result]:
    """A TIMEOUT result for each run that outlasted its deadline."""
    return [
        Result(name, TIMEOUT, f"run {run_id} still going", run_id)
        for name, run_id in pending.items()
    ]


def split_pending(results: list[Result]) -> tuple[list[Result], dict[str, int]]:
    """Separate the runs still going from the results already decided.

    Args:
        results: A sweep's results.

    Returns:
        (the results that are decided, fixture -> run id for each TIMEOUT that
        names a run it can be read from again).
    """
    pending = {
        result.fixture: result.run_id
        for result in results
        if result.state == TIMEOUT and result.run_id is not None
    }
    settled = [result for result in results if result.fixture not in pending]
    return settled, pending


def write_pending(
    path: Path, targets: list[str], settled: list[Result], pending: dict[str, int]
) -> None:
    """Record what the next invocation needs to finish this sweep.

    Args:
        path: The file to write.
        targets: Every fixture the sweep set out to run.
        settled: The results already decided, which the final verdict still needs.
        pending: Fixture -> run id for the runs still going.
    """
    record = {
        "targets": targets,
        "settled": [asdict(result) for result in settled],
        "pending": pending,
    }
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")


def read_pending(path: Path) -> tuple[list[str], list[Result], dict[str, int]]:
    """Load what `write_pending` recorded.

    Args:
        path: The file written by `write_pending`.

    Returns:
        (targets, settled results, fixture -> run id still going).

    Raises:
        ValueError: The file is missing, unreadable or not in the written shape.
    """
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        targets = [str(name) for name in record["targets"]]
        settled = [Result(**result) for result in record["settled"]]
        pending = {str(name): int(run) for name, run in record["pending"].items()}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"cannot read the pending runs from {path}: {exc}") from exc
    return targets, settled, pending


def collect_pending(pending: dict[str, int], timeout_minutes: int) -> list[Result]:
    """Wait for runs a first sweep left going, until each finishes or time is up.

    Args:
        pending: Fixture -> run id. Emptied as runs settle.
        timeout_minutes: How long to keep reading them.

    Returns:
        One Result per run: settled, or TIMEOUT when it was still going.
    """
    deadline = time.time() + timeout_minutes * 60
    misses: dict[str, int] = {}
    results: list[Result] = []
    while pending:
        results.extend(_read_pending(pending, misses))
        if not pending:
            break
        time.sleep(_POLL_SECONDS)
        if time.time() >= deadline:
            break
    results.extend(_still_going(pending))
    return results


def sweep_verdict(targets: list[str], results: list[Result]) -> tuple[int, list[str]]:
    """Turn per-fixture results into an exit code and a report.

    Args:
        targets: Fixture names the sweep set out to run.
        results: One per fixture that produced an answer.

    Returns:
        (exit code, report lines). 0 all green, 1 a fixture failed, 2 the
        sweep proved nothing -- it ran none, could not reach one, one was
        still going at the deadline, one was waiting on a release, or a
        rehearsal held one.
    """
    # Wide enough for the longest state, so the column stays a column.
    lines = [
        f"  {result.state:<15} {result.fixture} - {result.detail}" for result in results
    ]
    if not targets:
        return 2, ["ERROR: the sweep selected 0 fixtures - it proved nothing."]
    if not results:
        return 2, ["ERROR: the sweep ran 0 fixtures - it proved nothing."]

    unanswered = sorted(set(targets) - {result.fixture for result in results})
    if unanswered:
        lines.append(f"  NOT RUN: {', '.join(unanswered)}")
    # A proven failure outranks an inconclusive neighbour: reporting the sweep
    # as INCONCLUSIVE would hide a leaked gate behind a fixture that timed out.
    if any(result.state not in {PASS, *INCONCLUSIVE} for result in results):
        return 1, lines
    if unanswered or any(result.state in INCONCLUSIVE for result in results):
        return 2, lines
    return 0, lines


def _sweep(targets: list[fixture_fleet.Entry], timeout_minutes: int) -> list[Result]:
    """Dispatch every target, wait for all of them, and read each run.

    A fixture a rehearsal holds is re-read every poll and dispatched once the
    hold clears, so the free ones never wait behind it.
    """
    deadline = time.time() + timeout_minutes * 60
    # Not yet dispatched -> the branches holding it, "" until its first read.
    waiting: dict[str, str] = {entry["name"]: "" for entry in targets}
    pending: dict[str, int] = {}
    # Consecutive failed reads per pending run, so one gh error is not a verdict.
    misses: dict[str, int] = {}
    results: list[Result] = []

    while True:
        for name, previous in list(waiting.items()):
            try:
                branches = _rehearsal_branches(f"{fixture_fleet.ORG}/{name}")
            except HoldUnreadableError as exc:
                del waiting[name]
                results.append(Result(name, UNREACHABLE, str(exc)))
                continue
            if branches:
                holders = ", ".join(branches)
                if holders != previous:
                    print(
                        f"  {name} carries {holders} - a rehearsal holds it, waiting",
                        flush=True,
                    )
                waiting[name] = holders
                continue
            del waiting[name]
            started = _start(name, deadline)
            if isinstance(started, Result):
                results.append(started)
            else:
                pending[name] = started

        results.extend(_read_pending(pending, misses))
        if not (waiting or pending):
            break
        time.sleep(_POLL_SECONDS)
        # Gated after the sleep, so no poll or hold re-read starts past the deadline.
        if time.time() >= deadline:
            break

    results.extend(_still_going(pending))
    for name, holders in waiting.items():
        results.append(
            Result(
                name, HELD, f"{holders} still there at the deadline - never dispatched"
            )
        )
    return results


def main() -> int:
    """Sweep the fleet and report what it proved."""
    parser = argparse.ArgumentParser(description="Full-fleet E2E sweep")
    parser.add_argument("--only", default="", help="comma-separated fixture names")
    parser.add_argument("--language", default="", help="restrict to one language")
    # Under the 60-minute life of the GitHub App token the sweep reads runs with.
    parser.add_argument("--timeout-minutes", type=int, default=50)
    parser.add_argument(
        "--canary-timeout-minutes",
        type=int,
        default=90,
        help="how long to wait for a running runner-image canary before failing",
    )
    parser.add_argument(
        "--canary-only",
        action="store_true",
        help="wait for the runner-image canary, then exit without dispatching",
    )
    parser.add_argument(
        "--skip-canary",
        action="store_true",
        help="dispatch without waiting, after a --canary-only step has waited",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="name the targets, dispatch nothing"
    )
    parser.add_argument(
        "--pending-file",
        default="",
        help="write runs still going at the deadline here and exit 0 for --collect",
    )
    parser.add_argument(
        "--collect",
        default="",
        help="finish the sweep a --pending-file run left, and print the verdict",
    )
    args = parser.parse_args()

    if args.collect:
        return _collect(Path(args.collect), args.timeout_minutes)

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

    if not args.skip_canary:
        canary = wait_for_image_canary(
            args.canary_timeout_minutes, os.environ.get(CANARY_TOKEN_ENV) or None
        )
        if canary == CANARY_RUNNING:
            repo, workflow = rehearse_branch.IMAGE_CANARY
            print(
                f"::error::SWEEP NOT RUN: {workflow} on {repo} was still running "
                f"after {args.canary_timeout_minutes} min, and dispatching would "
                "cancel it. Nothing was dispatched - re-run the sweep once the "
                "canary finishes."
            )
            return 2
    if args.canary_only:
        print("Runner-image canary clear: the sweep may dispatch.")
        return 0

    results = _sweep(targets, args.timeout_minutes)
    if args.pending_file:
        settled, pending = split_pending(results)
        if pending:
            write_pending(Path(args.pending_file), names, settled, pending)
            set_github_output(pending="true")
            _print_results(sweep_verdict(names, results)[1])
            print(
                f"{len(pending)} run(s) still going: {', '.join(sorted(pending))}. "
                "The collect step reads them under a fresh token."
            )
            return 0
    return _report(names, results)


def _print_results(lines: list[str]) -> None:
    """Print the per-fixture report."""
    print("Sweep results:")
    for line in lines:
        print(line)


def _report(names: list[str], results: list[Result]) -> int:
    """Print the verdict on a finished sweep and return its exit code."""
    code, lines = sweep_verdict(names, results)
    _print_results(lines)
    if code == 0:
        print(f"FLEET GREEN: {len(results)} fixture(s) ran hyperi-ci@main and passed.")
    elif code == 1:
        print("FLEET RED: a fixture failed on current main - consumers are next.")
    else:
        print(
            "SWEEP INCONCLUSIVE: it did not prove main is safe. Read it, do not re-run blind."
        )
    return code


def _collect(path: Path, timeout_minutes: int) -> int:
    """Finish a sweep from its pending file and return the verdict's exit code."""
    try:
        names, settled, pending = read_pending(path)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"Collecting {len(pending)} run(s) still going: {', '.join(sorted(pending))}")
    return _report(names, settled + collect_pending(pending, timeout_minutes))


if __name__ == "__main__":
    sys.exit(main())
