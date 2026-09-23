#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/negative-cases.py
# Purpose:   Prove a planted failure still fails, and at the stage it declares
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Run the fleet's planted failures and refuse the ones that went green.

A sweep over clean fixtures proves a gate PASSES on clean input. It cannot
tell a blocking gate from a warn-only one, or from a gate that never ran:
all three are the same green tick. The `.ci-negative/` cases are trees that
DO carry the defect, and this runs them (issue #219).

Each case is data on the fixture's main: a `<case>.patch` that plants the
failure and a `<case>.yaml` that says what the run must do. The runner
applies the patch to an ephemeral `expect-fail/<case>` branch, opens a pull
request, and asserts the run failed at the declared stage for the declared
reason.

Four outcomes are RED, and the first is the dangerous one:

* `leaked`       - the planted failure shipped green, so the gate is off.
* `wrong-stage`  - something failed, but not the gate under test.
* `wrong-reason` - the declared stage failed without the declared tool
                   appearing in its log.
* `stale-patch`  - the patch no longer applies, so the case plants nothing.

A case whose gate is merged but not yet on PyPI declares `pending_release:
true` and reads INCONCLUSIVE rather than red. Inconclusive is not a pass: the
verdict refuses anything that is not PASS either way.

`--check-patches` answers the stale-patch half on its own, in seconds, with no
run dispatched anywhere. The full run does it first as well, so a patch that
plants nothing costs a clone rather than a pull request and 45 minutes.

A PULL REQUEST, not a bare push. hyperi-ci's plan job sets run-checks=false
for a push to a non-main branch, so a branch push finishes green in about
half a minute having run no gate at all -- which this would read as a leak.
The commit type matters for the same reason: a non-bumping type skips the
quality job.

Usage:
    uv run scripts/negative-cases.py
    uv run scripts/negative-cases.py --check-patches
    uv run scripts/negative-cases.py --only ci-test-manifests --dry-run
    uv run scripts/negative-cases.py --case hadolint-error --keep
    uv run scripts/negative-cases.py --cli-branch fix/my-gate
Exit 1 on a case that did not prove its gate, 2 when it proved nothing.
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_fleet  # noqa: E402

_SCRIPTS = Path(__file__).resolve().parent


def _load(stem: str, name: str):
    """Import a sibling script whose filename is not an importable module name."""
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / stem)
    assert spec is not None and spec.loader is not None  # a real file always resolves
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rehearse_branch = _load("rehearse-branch.py", "rehearse_branch")
sweep_fleet = _load("sweep-fleet.py", "sweep_fleet")

# The verdict is sweep-fleet's, unchanged: one place decides, and it refuses
# anything that is not PASS. These states are the inverted expectation.
PASS = sweep_fleet.PASS
TIMEOUT = sweep_fleet.TIMEOUT
UNREACHABLE = sweep_fleet.UNREACHABLE
PENDING_RELEASE = sweep_fleet.PENDING_RELEASE
LEAKED = "leaked"
WRONG_STAGE = "wrong-stage"
WRONG_REASON = "wrong-reason"
STALE_PATCH = "stale-patch"

CASE_DIR = ".ci-negative"
_POLL_SECONDS = 20
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Case:
    """One planted failure and what its run has to do."""

    fixture: str
    name: str
    patch: str
    branch: str
    stage: str
    reason: str
    pending_release: bool = False

    @property
    def case_id(self) -> str:
        """Fixture-qualified name, which is what the report rows key on."""
        return f"{self.fixture}/{self.name}"


@dataclass(slots=True)
class Live:
    """A case that has been pushed and is waiting on a run."""

    case: Case
    clone: Path
    head_sha: str = ""
    pr_number: int = 0
    run_id: int = 0


@dataclass(slots=True)
class Fixture:
    """A cloned fixture and what the runner changed on it."""

    repo: str
    clone: Path
    prior_override: str | None = None
    override_set: bool = False
    branches: list[str] = field(default_factory=list)


def _run(
    args: list[str], *, cwd: Path | None = None, timeout: int = 120
) -> subprocess.CompletedProcess:
    """subprocess.run with the repo's UTF-8 policy pinned."""
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def negative_fixtures(fleet: list[fixture_fleet.Entry]) -> list[fixture_fleet.Entry]:
    """Fleet entries declaring a `.ci-negative/` directory, in name order."""
    chosen = [entry for entry in fleet if entry.get("negative_cases")]
    return sorted(chosen, key=lambda entry: entry["name"])


def parse_case(fixture: str, name: str, text: str) -> Case | str:
    """Read one `<case>.yaml` contract, or say why it is not one.

    Args:
        fixture: Upstream fixture repo name.
        name: The case file's stem.
        text: The YAML contract.

    Returns:
        The case, or a string naming what the contract is missing. A contract
        that cannot be read proves nothing, so it is never silently skipped.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return f"unreadable YAML: {exc}"
    if not isinstance(data, dict):
        return "the contract is not a mapping"

    missing = [key for key in ("patch", "stage", "reason") if not data.get(key)]
    if missing:
        return f"the contract has no {', '.join(missing)}"
    expect = str(data.get("expect", "")).strip()
    if expect != "fail":
        return f"expect is {expect!r}; a negative case expects 'fail'"

    # Refused rather than coerced: `pending_release: "no"` is a truthy string,
    # and a typo that quietly softens a case is the failure this guards.
    pending = data.get("pending_release", False)
    if not isinstance(pending, bool):
        return f"pending_release must be true or false, got {pending!r}"

    return Case(
        fixture=fixture,
        name=str(data.get("case") or name),
        patch=str(data["patch"]),
        branch=str(data.get("branch") or f"expect-fail/{name}"),
        stage=str(data["stage"]),
        reason=str(data["reason"]),
        pending_release=pending,
    )


def read_cases(fixture: str, clone: Path) -> tuple[list[Case], list[str]]:
    """Every contract in a cloned fixture's `.ci-negative/`, plus its problems.

    Returns:
        (cases, problems). A fixture the fleet marks `negative_cases` that
        carries no contract is a problem, not an empty list -- the fleet would
        otherwise report a gate as proven by nothing.
    """
    directory = clone / CASE_DIR
    if not directory.is_dir():
        return [], [f"{fixture}: declares negative_cases but has no {CASE_DIR}/"]

    cases: list[Case] = []
    problems: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        parsed = parse_case(fixture, path.stem, path.read_text(encoding="utf-8"))
        if isinstance(parsed, str):
            problems.append(f"{fixture}/{path.stem}: {parsed}")
            continue
        if not (directory / parsed.patch).is_file():
            problems.append(f"{parsed.case_id}: patch {parsed.patch} is missing")
            continue
        cases.append(parsed)
    if not cases and not problems:
        problems.append(f"{fixture}: {CASE_DIR}/ carries no case contract")
    return cases, problems


def patch_applies(clone: Path, case: Case) -> str:
    """Empty string when the case's patch still applies, else why it does not.

    A patch whose context has moved plants nothing, so the run it produces
    proves no gate while still looking like a case in the catalogue.
    `rust-cve-advisory.patch` sat that way once the fixture's `src/main.rs`
    grew a hot path.

    Args:
        clone: The fixture checked out at its default branch.
        case: The contract naming the patch.

    Returns:
        The reason git refused it, or an empty string.
    """
    result = _run(
        ["git", "-C", str(clone), "apply", "--check", f"{CASE_DIR}/{case.patch}"],
        timeout=60,
    )
    if result.returncode == 0:
        return ""
    detail = (result.stderr or result.stdout).strip().splitlines()
    if not detail:
        return f"git apply --check exited {result.returncode}"
    return "; ".join(line.strip() for line in detail[-2:])


def tokens(text: str) -> set[str]:
    """Lowercase alphanumeric words, so `lint-manifests` and `Lint manifests` meet."""
    return set(_TOKEN.findall(text.lower()))


def stage_matches(stage: str, job: dict) -> bool:
    """Whether ``job`` is the declared stage.

    GitHub reports a job's DISPLAY name, never its YAML key, so a stage is
    matched by word: `quality` against `ci / Quality`, `lint-manifests`
    against the failed step `Lint manifests, charts and IaC`. Only FAILED
    steps are consulted -- a job that died before reaching the gate did not
    run the gate.
    """
    wanted = tokens(stage)
    if not wanted:
        return False
    if wanted <= tokens(str(job.get("name") or "")):
        return True
    return any(
        wanted <= tokens(str(step.get("name") or ""))
        for step in job.get("steps") or []
        if step.get("conclusion") == "failure"
    )


def classify(
    case: Case, conclusion: str, jobs: list[dict], logs: dict[int, str]
) -> tuple[str, str]:
    """What one finished run proved about its planted failure.

    Pure, so the dangerous outcome has a test rather than a run.

    Args:
        case: The contract the run has to satisfy.
        conclusion: The run's own conclusion.
        jobs: Its jobs, each with its steps.
        logs: Job id -> log text, for the jobs whose logs could be read.

    Returns:
        (state, detail).
    """
    if conclusion == "success":
        return LEAKED, "the run passed - the planted failure shipped green"
    if conclusion != "failure":
        return UNREACHABLE, f"the run concluded {conclusion!r}, which proves nothing"
    if not jobs:
        return UNREACHABLE, "the run reported no jobs"

    failed = [job for job in jobs if job.get("conclusion") == "failure"]
    if not failed:
        return UNREACHABLE, "the run failed but no job did"
    matched = [job for job in failed if stage_matches(case.stage, job)]
    if not matched:
        names = ", ".join(str(job.get("name")) for job in failed)
        return WRONG_STAGE, f"declared stage {case.stage!r}, failed at: {names}"

    # The reason has to appear in the log of the job that failed, not merely
    # somewhere in the run: a tool named in an unrelated job proves nothing.
    text = "\n".join(
        logs.get(int(job.get("databaseId") or 0), "") for job in matched
    ).strip()
    if not text:
        return UNREACHABLE, f"failed at {case.stage} but its log is unreadable"
    if case.reason.lower() not in text.lower():
        return (
            WRONG_REASON,
            f"failed at {case.stage} with no {case.reason!r} in its log",
        )
    return PASS, f"failed at {case.stage} on {case.reason}"


def soften(case: Case, state: str, detail: str) -> tuple[str, str]:
    """Read a pending-release case's RED verdict as inconclusive instead.

    A fixture takes its workflows from `@main` the instant they merge but its
    CLI from PyPI on release, so a case for a gate that has landed and not
    shipped fails for a reason that says nothing about the gate. Softening is
    never a pass -- PENDING_RELEASE is one of the verdict's inconclusive
    states, so the run still refuses to go green.

    Pure, so the declaration has a test rather than a release cycle.

    Args:
        case: The contract the run had to satisfy.
        state: What ``classify`` concluded.
        detail: Its reason line.

    Returns:
        (state, detail), unchanged unless the case declares pending_release.
    """
    if not case.pending_release:
        return state, detail
    if state == PASS:
        return PASS, f"{detail} - the gate shipped, drop pending_release"
    if state in (LEAKED, WRONG_STAGE, WRONG_REASON):
        return PENDING_RELEASE, f"{detail} - declared pending_release"
    return state, detail


def _clone(repo: str, into: Path, *, depth: int = 0) -> str:
    """Clone a fixture. Empty string on success, else the reason.

    Args:
        repo: Upstream `owner/name`.
        into: Destination directory.
        depth: Shallow-clone depth. 0 takes the full history the push path
            needs; the patch check only reads the working tree.

    Returns:
        The reason the clone failed, or an empty string.
    """
    args = ["gh", "repo", "clone", repo, str(into)]
    if depth:
        args += ["--", "--depth", str(depth)]
    result = _run(args, timeout=300)
    if result.returncode != 0:
        return result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "?"
    # Per-clone, because a runner has GH_TOKEN but no git credential helper, and
    # the push below would ask for a password nobody is there to type. A
    # workstation already has this globally; setting it again costs nothing.
    _run(
        [
            "git",
            "-C",
            str(into),
            "config",
            "credential.helper",
            "!gh auth git-credential",
        ]
    )
    return ""


def _push_case(fixture: Fixture, case: Case) -> tuple[str, str]:
    """Plant the failure on its branch and push it. (head sha, error)."""
    clone = fixture.clone
    # A leftover branch from an interrupted run would otherwise need a force
    # push; deleting first keeps every push a fast-forward.
    _run(["git", "-C", str(clone), "push", "origin", "--delete", case.branch])
    steps = [
        ["checkout", "-B", case.branch, "origin/HEAD"],
        ["apply", f"{CASE_DIR}/{case.patch}"],
        ["add", "-A"],
        # A non-bumping commit type skips the quality job, which would read as
        # a leak. `fix` is the cheapest release-worthy type.
        ["commit", "-m", f"fix({case.name}): plant the negative case [do not merge]"],
        ["push", "origin", case.branch],
    ]
    for git_args in steps:
        result = _run(["git", "-C", str(clone), *git_args], timeout=120)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            return "", f"git {git_args[0]} failed: {detail[-1] if detail else '?'}"
    fixture.branches.append(case.branch)
    head = _run(["git", "-C", str(clone), "rev-parse", "HEAD"])
    return head.stdout.strip(), ""


def _open_pr(repo: str, case: Case) -> tuple[int, str]:
    """Open the draft PR whose run is the evidence. (number, error)."""
    result = _run(
        [
            "gh",
            "pr",
            "create",
            "-R",
            repo,
            "--draft",
            "--base",
            "main",
            "--head",
            case.branch,
            "--title",
            f"ci: negative case {case.name} [do not merge]",
            "--body",
            (
                f"Throwaway negative case opened by scripts/negative-cases.py. "
                f"It plants {CASE_DIR}/{case.patch} so the run FAILS at the "
                f"{case.stage} stage on {case.reason}. A green run here means "
                "the gate is off. Never merged; cleaned up automatically."
            ),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip().splitlines()
        return 0, f"PR create failed: {stderr[-1] if stderr else '?'}"
    return int(result.stdout.strip().rstrip("/").rsplit("/", 1)[-1]), ""


def pick_run(runs: list[dict], head_sha: str) -> int | None:
    """The run for ``head_sha``, or None when the list holds no such run.

    Matched on the COMMIT, never on the branch alone: `expect-fail/<case>` is
    reused every cycle, so a previous cycle's run answers a branch query and
    the verdict would certify a run this invocation never started.
    """
    for run in runs:
        if head_sha and str(run.get("headSha")) == head_sha:
            return int(run["databaseId"])
    return None


def _pr_run_id(repo: str, branch: str, head_sha: str) -> int | None:
    """The pull_request run for ``head_sha``, or None until it appears."""
    listed = _run(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--branch",
            branch,
            "--event",
            "pull_request",
            "--json",
            "databaseId,headSha",
            "--limit",
            "10",
        ],
        timeout=60,
    )
    if listed.returncode != 0:
        return None
    try:
        return pick_run(json.loads(listed.stdout or "[]"), head_sha)
    except json.JSONDecodeError:
        return None


def _failed_logs(repo: str, jobs: list[dict]) -> dict[int, str]:
    """Each failed job's log, keyed by job id.

    Per JOB through the REST endpoint, not `gh run view --log-failed`: that
    returns exit 0 and an EMPTY body for a run whose failing job came from a
    reusable workflow, which is every hyperi-ci consumer. Measured on
    ci-test-manifests run 35819839750, where the same log reads fine here.
    """
    found: dict[int, str] = {}
    for job in jobs:
        if job.get("conclusion") != "failure":
            continue
        job_id = int(job.get("databaseId") or 0)
        if not job_id:
            continue
        viewed = _run(
            ["gh", "api", f"/repos/{repo}/actions/jobs/{job_id}/logs"], timeout=300
        )
        if viewed.returncode == 0 and viewed.stdout.strip():
            found[job_id] = viewed.stdout
    return found


def check_patches(
    fixtures: dict[str, Fixture], cases: list[Case]
) -> list[sweep_fleet.Result]:
    """`git apply --check` every case against its fixture's default branch.

    Args:
        fixtures: Fixture name -> the clone its cases live in.
        cases: Contracts to check.

    Returns:
        One result per case: PASS, or STALE_PATCH naming what git refused.
    """
    results: list[sweep_fleet.Result] = []
    for case in cases:
        reason = patch_applies(fixtures[case.fixture].clone, case)
        if reason:
            results.append(sweep_fleet.Result(case.case_id, STALE_PATCH, reason))
        else:
            results.append(
                sweep_fleet.Result(case.case_id, PASS, f"{case.patch} still applies")
            )
    return results


def _prepare(
    fixtures: dict[str, Fixture], cases: list[Case]
) -> tuple[list[Live], list[sweep_fleet.Result]]:
    """Push every case and open its PR, reporting the ones that never started."""
    live: list[Live] = []
    results: list[sweep_fleet.Result] = []
    for case in cases:
        fixture = fixtures[case.fixture]
        head_sha, reason = _push_case(fixture, case)
        if reason:
            results.append(sweep_fleet.Result(case.case_id, UNREACHABLE, reason))
            continue
        pr_number, error = _open_pr(fixture.repo, case)
        if error:
            results.append(sweep_fleet.Result(case.case_id, UNREACHABLE, error))
            continue
        live.append(
            Live(
                case=case,
                clone=fixture.clone,
                head_sha=head_sha,
                pr_number=pr_number,
            )
        )
        # Flushed because the poll below runs for tens of minutes, and a job
        # log that says nothing until the end is a job nobody can follow.
        print(f"  planted {case.case_id} -> {fixture.repo}#{pr_number}", flush=True)
    return live, results


def _await_runs(
    fixtures: dict[str, Fixture], live: list[Live], deadline: float
) -> list[sweep_fleet.Result]:
    """Wait for each case's PR run, then read and classify it."""
    results: list[sweep_fleet.Result] = []
    waiting = list(live)
    while waiting and time.time() < deadline:
        for item in list(waiting):
            repo = fixtures[item.case.fixture].repo
            if not item.run_id:
                found = _pr_run_id(repo, item.case.branch, item.head_sha)
                if found is None:
                    continue
                item.run_id = found
                print(f"  {item.case.case_id} -> run {found}", flush=True)
            state = sweep_fleet._run_state(repo, item.run_id)
            if state is None:
                waiting.remove(item)
                results.append(
                    sweep_fleet.Result(
                        item.case.case_id, UNREACHABLE, f"cannot read run {item.run_id}"
                    )
                )
                continue
            if state[0] != "completed":
                continue
            waiting.remove(item)
            jobs = sweep_fleet._read_jobs(repo, item.run_id)
            if jobs is None:
                results.append(
                    sweep_fleet.Result(
                        item.case.case_id,
                        UNREACHABLE,
                        f"cannot read run {item.run_id} jobs",
                    )
                )
                continue
            verdict, detail = soften(
                item.case,
                *classify(item.case, state[1], jobs, _failed_logs(repo, jobs)),
            )
            results.append(
                sweep_fleet.Result(
                    item.case.case_id, verdict, f"run {item.run_id}: {detail}"
                )
            )
        if waiting:
            time.sleep(_POLL_SECONDS)

    for item in waiting:
        detail = f"run {item.run_id} still going" if item.run_id else "no run appeared"
        results.append(sweep_fleet.Result(item.case.case_id, TIMEOUT, detail))
    return results


def _cleanup(fixtures: dict[str, Fixture], live: list[Live]) -> None:
    """Close every PR, delete every branch, and put each override back."""
    for item in live:
        repo = fixtures[item.case.fixture].repo
        _run(["gh", "pr", "close", str(item.pr_number), "-R", repo], timeout=60)
    for fixture in fixtures.values():
        for branch in fixture.branches:
            result = _run(
                ["git", "-C", str(fixture.clone), "push", "origin", "--delete", branch],
                timeout=60,
            )
            if result.returncode != 0:
                print(f"  WARNING: {fixture.repo} {branch} NOT deleted - delete it")
        if not fixture.override_set:
            continue
        # ci-test-manifests carries a permanent override pinning @main, so this
        # restores the prior value rather than deleting the key.
        if fixture.prior_override is None:
            rehearse_branch._gh_var(fixture.repo, "delete")
        else:
            rehearse_branch._gh_var(fixture.repo, "set", fixture.prior_override)


def _pin_cli(fixture: Fixture, branch: str) -> str:
    """Point the fixture's CLI at a hyperi-ci branch. Empty string on success.

    A fixture takes its workflows from `@main` the moment they merge but its
    CLI from PyPI on release, so a case for an unreleased CLI gate fails for
    the wrong reason until this is set.
    """
    fixture.prior_override = rehearse_branch._read_override(fixture.repo)
    if not rehearse_branch._gh_var(
        fixture.repo, "set", rehearse_branch.override_value(branch)
    ):
        return f"could not set HYPERCI_INSTALL_OVERRIDE on {fixture.repo}"
    fixture.override_set = True
    return ""


def _report_patches(code: int, lines: list[str]) -> int:
    """Print the patch-check report and hand its exit code back.

    Args:
        code: What ``sweep_verdict`` returned.
        lines: Its report lines.

    Returns:
        ``code``, so the caller can return the call directly.
    """
    print("Patch check:")
    for line in lines:
        print(line)
    if code == 0:
        print("PATCHES APPLY: every case still plants its defect.")
    elif code == 1:
        print(
            "STALE PATCH: a case plants nothing, so the gate it names is proven "
            "by nothing. Move the patch context, NEVER the planted defect."
        )
    else:
        print("INCONCLUSIVE: a case could not be read. Read it, do not re-run blind.")
    return code


def main() -> int:
    """Run the fleet's planted failures and report which gates they proved."""
    parser = argparse.ArgumentParser(description="Negative-case runner")
    parser.add_argument("--only", default="", help="comma-separated fixture names")
    parser.add_argument("--case", default="", help="comma-separated case names")
    parser.add_argument("--timeout-minutes", type=int, default=45)
    parser.add_argument(
        "--cli-branch",
        default="",
        help="pin each fixture's CLI to this hyperi-ci branch for the run",
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the PRs and branches in place"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="name the cases, push nothing"
    )
    parser.add_argument(
        "--check-patches",
        action="store_true",
        help="git apply --check every case and stop; dispatches no run",
    )
    args = parser.parse_args()

    fleet = fixture_fleet.load_fleet()
    entries = negative_fixtures(fleet)
    only = [name.strip() for name in args.only.split(",") if name.strip()]
    wanted = [name.strip() for name in args.case.split(",") if name.strip()]

    unknown = sorted(set(only) - fixture_fleet.declared_names(fleet))
    if unknown:
        print(f"ERROR: not in config/fixtures.yaml: {', '.join(unknown)}")
        return 2
    if only:
        entries = [entry for entry in entries if entry["name"] in only]
    if not entries:
        print("ERROR: no fixture declares negative_cases - nothing to prove.")
        return 2

    deadline = time.time() + args.timeout_minutes * 60
    with tempfile.TemporaryDirectory(prefix="negative-cases-") as tmp:
        fixtures: dict[str, Fixture] = {}
        cases: list[Case] = []
        problems: list[str] = []

        for entry in entries:
            name = entry["name"]
            repo = f"{fixture_fleet.ORG}/{name}"
            clone = Path(tmp) / name
            if reason := _clone(repo, clone, depth=1 if args.check_patches else 0):
                problems.append(f"{name}: clone failed: {reason}")
                continue
            fixtures[name] = Fixture(repo=repo, clone=clone)
            found, trouble = read_cases(name, clone)
            cases.extend(found)
            problems.extend(trouble)

        if wanted:
            cases = [case for case in cases if case.name in wanted]
            missing = sorted(set(wanted) - {case.name for case in cases})
            if missing:
                print(f"ERROR: no such case: {', '.join(missing)}")
                return 2

        targets = [case.case_id for case in cases]
        print(f"Negative cases: {len(targets)} - {', '.join(targets) or 'none'}")
        for problem in problems:
            print(f"  PROBLEM: {problem}")

        if args.check_patches:
            return _report_patches(
                *sweep_fleet.sweep_verdict(
                    targets + problems, check_patches(fixtures, cases)
                )
            )
        if args.dry_run:
            print("--dry-run: nothing pushed")
            return 0 if targets and not problems else 2
        if not targets:
            print("ERROR: no runnable case - the run proved nothing.")
            return 2

        # Cheap first. A patch that no longer applies plants nothing, so it
        # earns its verdict here instead of a pull request and a 45-minute wait.
        stale = [
            result for result in check_patches(fixtures, cases) if result.state != PASS
        ]
        for result in stale:
            print(f"  STALE PATCH: {result.fixture} - {result.detail}", flush=True)
        skipped = {result.fixture for result in stale}
        runnable = [case for case in cases if case.case_id not in skipped]

        live: list[Live] = []
        results: list[sweep_fleet.Result] = list(stale)
        try:
            if args.cli_branch:
                for fixture in fixtures.values():
                    if reason := _pin_cli(fixture, args.cli_branch):
                        print(f"ERROR: {reason}")
                        return 2
                print(f"CLI pinned to hyperi-ci@{args.cli_branch}")
            live, prepared = _prepare(fixtures, runnable)
            results.extend(prepared)
            results.extend(_await_runs(fixtures, live, deadline))
        finally:
            if args.keep:
                print("--keep: leaving the PRs, branches and overrides in place")
            else:
                _cleanup(fixtures, live)

    # A contract that could not be read is an unanswered target, which the
    # verdict already refuses to call a pass.
    code, lines = sweep_fleet.sweep_verdict(targets + problems, results)
    print("Negative-case results:")
    for line in lines:
        print(line)
    if code == 0:
        print(f"GATES PROVEN: {len(results)} planted failure(s) failed as declared.")
    elif code == 1:
        print(
            "GATE NOT PROVEN: a planted failure went green or failed elsewhere - "
            "the gate it tests is not doing its job."
        )
    else:
        print("INCONCLUSIVE: it did not prove any gate. Read it, do not re-run blind.")
    waiting = [result.fixture for result in results if result.state == PENDING_RELEASE]
    if waiting:
        print(f"  waiting on a CLI release: {', '.join(waiting)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
