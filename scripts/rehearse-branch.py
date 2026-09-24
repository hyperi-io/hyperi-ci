#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/rehearse-branch.py
# Purpose:   Rehearse a hyperi-ci branch against a real fixture repo
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rehearse a hyperi-ci BRANCH against a ci-test-* fixture BEFORE merging.

Closes the self-usage loop (docs/plans/2026-07-branch-mode, decision 5b):
consumers pin the reusable workflows @main and install the CLI from PyPI,
so an ordinary fixture run can only ever validate what has already
shipped. This script points a throwaway fixture branch at the CANDIDATE:

1. clones the fixture, creates ``rehearse/<slug>``,
2. swaps every ``hyperi-io/hyperi-ci/...@main`` ref in the fixture's
   workflows to ``@<branch>``,
3. sets the fixture's ``HYPERCI_INSTALL_OVERRIDE`` repo variable to
   ``uvx --from git+https://github.com/hyperi-io/hyperi-ci@<branch>
   hyperi-ci`` so the branch's CLI runs too (not the released one),
4. pushes the branch and opens a DRAFT pull request -- the pull_request
   run exercises the branch's workflows + CLI through quality / test /
   build / container (a dev push lands in the prunable ``branch-*``
   namespace on an opted-in fixture),
5. watches the run, reports per-job outcomes, writes the verdict into the
   fixture PR body as a rehearsal RECORD, then cleans up (closes the PR,
   restores the variable, best-effort deletes the branch).

The record is what makes the rehearsal a gate rather than a habit: it
names the hyperi-ci commit that was rehearsed and the run that proved it,
so ``scripts/rehearse-gate.py`` can refuse a PR whose current head has
never been run against a fixture (issue #215).

Deliberately NEVER: merges anything, touches the fixture's main, or
publishes. Known limit (accepted, pinning decision #31 gate-only):
composite refs INSIDE the lang workflows stay @main -- composite changes
are covered by hyperi-ci's own local-ref ci.yml instead.

Usage:
    uv run scripts/rehearse-branch.py --branch fix/my-change \
        --repo hyperi-io/ci-test-go-app [--keep] [--no-cli-override] \
        [--timeout-minutes 20]
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HYPERI_CI_REPO = "hyperi-io/hyperi-ci"
_REF_SWAP = re.compile(r"(hyperi-io/hyperi-ci/[^@\s]+)@main\b")


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
    )


def swap_refs(text: str, branch: str) -> tuple[str, int]:
    """Swap every hyperi-io/hyperi-ci ...@main ref to @<branch>.

    Returns (new_text, swap_count). Pure function -- unit-tested.
    """
    new_text, count = _REF_SWAP.subn(rf"\1@{branch}", text)
    return new_text, count


def rehearse_slug(branch: str) -> str:
    """Git-ref-safe slug of the candidate branch name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-.")[:80]


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def _branch_head(branch: str) -> str | None:
    """The branch's head commit on hyperi-ci, or None when it is not pushed."""
    result = _run(
        [
            "gh",
            "api",
            f"/repos/{_HYPERI_CI_REPO}/branches/{branch}",
            "--jq",
            ".commit.sha",
        ]
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


_OVERRIDE_VAR = "HYPERCI_INSTALL_OVERRIDE"


class OverrideUnreadableError(RuntimeError):
    """The fixture's install override could not be read, so it cannot be restored."""


def _read_override(repo: str) -> str | None:
    """The fixture's current HYPERCI_INSTALL_OVERRIDE, or None when it is unset.

    Raises:
        OverrideUnreadableError: gh failed for any reason other than the
            variable not existing. Read as "unset", cleanup would DELETE a
            permanent override it merely failed to read.
    """
    result = _run(
        [
            "gh",
            "api",
            f"/repos/{repo}/actions/variables/{_OVERRIDE_VAR}",
            "--jq",
            ".value",
        ]
    )
    if result.returncode == 0:
        return result.stdout.strip() or None
    if "404" in result.stderr or "Not Found" in result.stderr:
        return None
    detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
    raise OverrideUnreadableError(
        f"cannot read {_OVERRIDE_VAR} on {repo}: {detail or f'gh exited {result.returncode}'}"
    )


def override_value(branch: str) -> str:
    """The HYPERCI_INSTALL_OVERRIDE that runs a hyperi-ci branch's own CLI.

    Without `--no-cache --refresh`, uvx resolves the branch to whatever it
    built last time and the record would certify a commit no fixture ran.
    `--python` pins the CLI's interpreter (issue #157).
    """
    return (
        "uvx --python 3.14 --no-cache --refresh --from "
        f"git+https://github.com/{_HYPERI_CI_REPO}@{branch} hyperi-ci"
    )


def _gh_var(repo: str, action: str, value: str = "") -> bool:
    """Set or delete HYPERCI_INSTALL_OVERRIDE on the fixture. True on success."""
    if action == "set":
        cmd = ["gh", "variable", "set", _OVERRIDE_VAR, "--body", value, "-R", repo]
    else:
        cmd = ["gh", "variable", "delete", _OVERRIDE_VAR, "-R", repo]
    result = _run(cmd)
    if result.returncode != 0 and action == "set":
        print(result.stderr, file=sys.stderr)
    return result.returncode == 0


_PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})


def summarise_jobs(jobs: list[dict]) -> tuple[bool, list[str]]:
    """Per-job verdict for a finished run: (every job passed, one line per job).

    Pure function -- unit-tested. A run with no jobs has proven nothing, so it
    does not pass.
    """
    lines = [
        f"  {str(job.get('conclusion') or 'pending'):<9} {job.get('name')}"
        for job in jobs
    ]
    passed = bool(jobs) and all(
        job.get("conclusion") in _PASSING_CONCLUSIONS for job in jobs
    )
    return passed, lines


RECORD_MARKER = "Rehearsal record"
_RECORD_FIELD = re.compile(
    r"^(hyperi-ci-sha|run-id|verdict):\s*(\S+)\s*$", re.MULTILINE
)


def record_block(sha: str, run_id: int, verdict: str) -> str:
    """The rehearsal record appended to the fixture PR body.

    Args:
        sha: The hyperi-ci commit that was rehearsed.
        run_id: The fixture run that proved it.
        verdict: pass, fail or timeout.

    Returns:
        A plain-text block the gate parses and a human can read.
    """
    return (
        f"\n\n{RECORD_MARKER}\n"
        f"hyperi-ci-sha: {sha}\n"
        f"run-id: {run_id}\n"
        f"verdict: {verdict}\n"
    )


def parse_record(body: str | None) -> dict[str, str] | None:
    """Read a rehearsal record out of a fixture PR body.

    Returns:
        The three fields, or None when the body carries no complete record.
    """
    if not body or RECORD_MARKER not in body:
        return None
    tail = body[body.rindex(RECORD_MARKER) :]
    found = dict(_RECORD_FIELD.findall(tail))
    if {"hyperi-ci-sha", "run-id", "verdict"} - found.keys():
        return None
    return found


def _write_record(repo: str, pr_number: int, body: str, block: str) -> bool:
    """Append the record to the fixture PR body. True on success."""
    result = _run(
        [
            "gh",
            "api",
            f"/repos/{repo}/pulls/{pr_number}",
            "-X",
            "PATCH",
            "-f",
            f"body={body}{block}",
        ],
        timeout=60,
    )
    if result.returncode != 0:
        print(f"WARNING: could not write the rehearsal record: {result.stderr.strip()}")
    return result.returncode == 0


def _wait_for_merge_ref(repo: str, pr_number: int, timeout_secs: int = 120) -> bool:
    """Wait until GitHub has computed ``refs/pull/N/merge`` for a new PR.

    GitHub computes the merge ref asynchronously, but fires the pull_request
    workflow on PR creation, so a run can reach checkout before the ref it is
    told to fetch exists. actions/checkout retries three times over ~20s and
    then fails the job with "couldn't find remote ref", which reads as the
    rehearsed branch being broken when nothing was ever fetched.

    Returns True once the ref resolves, False on timeout.
    """
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        probe = _run(
            ["gh", "api", f"repos/{repo}/git/ref/pull/{pr_number}/merge"], timeout=60
        )
        if probe.returncode == 0:
            return True
        time.sleep(5)
    return False


def _rerun(repo: str, run_id: int, settle_secs: int = 90) -> bool:
    """Re-run a failed rehearsal run once and wait for it to leave `completed`.

    Without the wait the caller's watcher reads the run's stale conclusion and
    returns the failure it was asked to retry.

    Returns True once the run is running again.
    """
    if (
        _run(["gh", "run", "rerun", str(run_id), "-R", repo], timeout=60).returncode
        != 0
    ):
        return False
    deadline = time.time() + settle_secs
    while time.time() < deadline:
        viewed = _run(
            ["gh", "run", "view", str(run_id), "-R", repo, "--json", "status"],
            timeout=60,
        )
        if viewed.returncode == 0:
            try:
                if json.loads(viewed.stdout or "{}").get("status") != "completed":
                    return True
            except json.JSONDecodeError:
                pass
        time.sleep(5)
    return False


def raced_the_merge_ref(jobs: list[dict]) -> bool:
    """True when every failed job died at its checkout step.

    That is the one failure a rerun can tell apart from a broken branch: the
    run started before GitHub computed refs/pull/N/merge (issue #260). Any
    other failure is the branch's result, and rerunning it would let a flaky
    pass stand in for the red it hid.
    """
    failed = [job for job in jobs if job.get("conclusion") == "failure"]
    if not failed:
        return False
    for job in failed:
        step = next(
            (s for s in job.get("steps", []) if s.get("conclusion") == "failure"),
            None,
        )
        if step is None or "checkout" not in step.get("name", "").lower():
            return False
    return True


def _failed_at_checkout(repo: str, run_id: int) -> bool:
    """Read a finished run's jobs and apply :func:`raced_the_merge_ref`."""
    viewed = _run(
        ["gh", "run", "view", str(run_id), "-R", repo, "--json", "jobs"], timeout=60
    )
    if viewed.returncode != 0:
        return False
    try:
        jobs = json.loads(viewed.stdout or "{}").get("jobs", [])
    except json.JSONDecodeError:
        return False
    return raced_the_merge_ref(jobs)


def _cancel_inflight(repo: str, rehearse_ref: str) -> list[int]:
    """Cancel every unfinished run on the rehearsal branch; return their ids.

    Closing the PR while a pull_request run is still queued leaves that run to
    start against a merge ref that no longer exists, so it dies red at checkout
    with nothing pointing at the teardown -- after a PASS as well as a timeout
    (issue #260). A cancelled run at least reads as abandoned.
    """
    listed = _run(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--branch",
            rehearse_ref,
            "--json",
            "databaseId,status",
            "--limit",
            "20",
        ],
        timeout=60,
    )
    if listed.returncode != 0:
        return []
    try:
        runs = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return []
    cancelled: list[int] = []
    for run in runs:
        if run.get("status") == "completed":
            continue
        run_id = int(run["databaseId"])
        cancel = _run(["gh", "run", "cancel", str(run_id), "-R", repo], timeout=60)
        if cancel.returncode == 0:
            cancelled.append(run_id)
    return cancelled


def pick_run(runs: list[dict], fixture_sha: str) -> dict | None:
    """The newest run built from ``fixture_sha``, or None.

    Every cycle reuses the rehearsal BRANCH NAME, and a deleted branch still
    matches `gh run list --branch`, so the newest run on that name can belong
    to a previous cycle. Selecting on the fixture commit is exact where the
    name is not: a stale green run read as this cycle's would certify a
    hyperi-ci commit no fixture ever ran (issue #263).
    """
    return next((r for r in runs if r.get("headSha") == fixture_sha), None)


def _watch_pr_run(
    repo: str, rehearse_ref: str, fixture_sha: str, timeout_minutes: int
) -> tuple[str, list[str], int]:
    """Wait for the rehearsal's pull_request run and read it job by job.

    Uses `gh run list` / `gh run view`, which every supported gh has, rather
    than `gh pr checks --json`, which older gh rejects outright.

    Args:
        repo: The fixture, ``owner/name``.
        rehearse_ref: The rehearsal branch pushed to it.
        fixture_sha: The commit this cycle pushed -- runs from any other are
            a previous cycle's and are ignored.
        timeout_minutes: How long to wait for a run to finish.

    Returns:
        ("pass" | "fail" | "timeout", lines, run id). A timeout is no verdict at
        all, and carries run id 0.

    """
    deadline = time.time() + timeout_minutes * 60
    last_error = ""
    while time.time() < deadline:
        listed = _run(
            [
                "gh",
                "run",
                "list",
                "-R",
                repo,
                "--branch",
                rehearse_ref,
                "--event",
                "pull_request",
                "--json",
                "databaseId,status,headSha",
                "--limit",
                "20",
            ],
            timeout=60,
        )
        candidates: list[dict] = []
        if listed.returncode == 0:
            try:
                candidates = json.loads(listed.stdout or "[]")
            except json.JSONDecodeError:
                last_error = "unreadable gh run list output"
        else:
            stderr_lines = listed.stderr.strip().splitlines()
            last_error = (
                stderr_lines[-1] if stderr_lines else f"gh exited {listed.returncode}"
            )
        mine = pick_run(candidates, fixture_sha)
        runs = [mine] if mine else []
        if runs and runs[0].get("status") == "completed":
            viewed = _run(
                [
                    "gh",
                    "run",
                    "view",
                    str(runs[0]["databaseId"]),
                    "-R",
                    repo,
                    "--json",
                    "jobs",
                ],
                timeout=60,
            )
            if viewed.returncode == 0:
                jobs = json.loads(viewed.stdout or "{}").get("jobs", [])
                passed, lines = summarise_jobs(jobs)
                return ("pass" if passed else "fail"), lines, int(runs[0]["databaseId"])
            last_error = viewed.stderr.strip()
        time.sleep(30)
    note = f" (last gh error: {last_error})" if last_error else ""
    return "timeout", [f"  no PR run finished within {timeout_minutes} min{note}"], 0


def main() -> int:
    """Run a hyperi-ci candidate branch against a fixture repo and report."""
    parser = argparse.ArgumentParser(
        description="Rehearse a hyperi-ci branch against a fixture repo"
    )
    parser.add_argument("--branch", required=True, help="hyperi-ci branch to rehearse")
    parser.add_argument("--repo", required=True, help="fixture repo (org/name)")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the rehearsal PR/branch/variable in place for inspection",
    )
    parser.add_argument(
        "--no-cli-override",
        action="store_true",
        help="rehearse workflows only; keep the released PyPI CLI",
    )
    parser.add_argument("--timeout-minutes", type=int, default=20)
    args = parser.parse_args()

    branch, repo = args.branch, args.repo

    if repo == _HYPERI_CI_REPO:
        return _fail("rehearse against a fixture, not hyperi-ci itself")
    if branch in ("main", "master"):
        return _fail("rehearsing main is meaningless — it is what fixtures already run")
    head_sha = _branch_head(branch)
    if head_sha is None:
        return _fail(
            f"branch {branch!r} not found on {_HYPERI_CI_REPO} — push it first"
        )
    print(f"Rehearsing {branch} at {head_sha[:12]} against {repo}")

    slug = rehearse_slug(branch)
    rehearse_ref = f"rehearse/{slug}"

    # A fixture may carry a permanent override (ci-test-manifests pins @main),
    # which cleanup restores rather than deletes. Read before anything is
    # pushed, so an unreadable one stops the run with nothing to undo.
    try:
        prior_override = _read_override(repo)
    except OverrideUnreadableError as exc:
        return _fail(f"{exc} -- not replacing an override this run could not restore")

    with tempfile.TemporaryDirectory(prefix="rehearse-") as tmp:
        clone = Path(tmp) / "fixture"
        result = _run(
            ["gh", "repo", "clone", repo, str(clone), "--", "--depth", "1"],
            timeout=300,
        )
        if result.returncode != 0:
            return _fail(f"clone failed: {result.stderr.strip()}")

        swapped_files = 0
        total_swaps = 0
        for wf in sorted((clone / ".github" / "workflows").glob("*.yml")):
            new_text, count = swap_refs(wf.read_text(encoding="utf-8"), branch)
            if count:
                wf.write_text(new_text, encoding="utf-8", newline="\n")
                swapped_files += 1
                total_swaps += count
        if total_swaps == 0:
            return _fail(f"{repo} has no hyperi-io/hyperi-ci@main refs to swap")
        print(f"Swapped {total_swaps} ref(s) in {swapped_files} file(s) -> @{branch}")

        for git_args in (
            ["checkout", "-b", rehearse_ref],
            ["add", ".github/workflows"],
            ["commit", "-m", f"ci: rehearse hyperi-ci@{branch}"],
            ["push", "origin", rehearse_ref],
        ):
            result = _run(["git", "-C", str(clone), *git_args], timeout=120)
            if result.returncode != 0:
                return _fail(f"git {git_args[0]} failed: {result.stderr.strip()}")

        # Identifies THIS cycle's runs. The branch name cannot -- every cycle
        # reuses it, and a deleted branch still matches `gh run list`.
        rev = _run(["git", "-C", str(clone), "rev-parse", "HEAD"], timeout=60)
        if rev.returncode != 0:
            return _fail(f"could not read the rehearsal commit: {rev.stderr.strip()}")
        fixture_sha = rev.stdout.strip()
        print(f"Fixture commit: {fixture_sha[:12]}")

        override_set = False
        if not args.no_cli_override:
            override_set = _gh_var(repo, "set", override_value(branch))
            if not override_set:
                return _fail("could not set HYPERCI_INSTALL_OVERRIDE")
            print(f"HYPERCI_INSTALL_OVERRIDE set on {repo}")

        body = (
            "Throwaway rehearsal PR created by scripts/rehearse-branch.py. "
            f"Exercises hyperi-ci@{branch} workflows"
            + ("" if args.no_cli_override else " + branch CLI")
            + " against this fixture. Never merged; cleaned up automatically."
        )
        result = _run(
            [
                "gh",
                "pr",
                "create",
                "-R",
                repo,
                "--draft",
                "--head",
                rehearse_ref,
                "--title",
                f"ci: rehearse hyperi-ci@{branch} [do not merge]",
                "--body",
                body,
            ],
            timeout=60,
        )
        if result.returncode != 0:
            return _fail(f"PR create failed: {result.stderr.strip()}")
        pr_url = result.stdout.strip()
        pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        print(f"Rehearsal PR: {pr_url}")

        if not _wait_for_merge_ref(repo, pr_number):
            print(
                f"WARNING: refs/pull/{pr_number}/merge did not appear - the run may "
                "fail at checkout through no fault of the branch"
            )

        verdict, lines, run_id = _watch_pr_run(
            repo, rehearse_ref, fixture_sha, args.timeout_minutes
        )

        # A run that started before the merge ref existed failed at checkout, not
        # on the branch. The ref is there now, so that one failure gets one
        # rerun; every other failure is reported as the branch's result.
        if (
            verdict == "fail"
            and run_id
            and _failed_at_checkout(repo, run_id)
            and _rerun(repo, run_id)
        ):
            print(f"Re-running {run_id} once - the first attempt raced the merge ref")
            verdict, lines, run_id = _watch_pr_run(
                repo, rehearse_ref, fixture_sha, args.timeout_minutes
            )

        print("Rehearsal run results:")
        for line in lines:
            print(line)

        # Written BEFORE cleanup: closing the PR leaves the body readable, and
        # the gate reads this to decide whether a hyperi-ci commit was proven.
        if args.no_cli_override:
            print("--no-cli-override: no record written, the CLI half was not tested")
        else:
            _write_record(
                repo, pr_number, body, record_block(head_sha, run_id, verdict)
            )

        if args.keep:
            print("--keep: leaving PR, branch, and override in place")
        else:
            cancelled = _cancel_inflight(repo, rehearse_ref)
            if cancelled:
                print(f"Cancelled unfinished run(s) before teardown: {cancelled}")
            _run(["gh", "pr", "close", str(pr_number), "-R", repo], timeout=60)
            if override_set:
                if prior_override is None:
                    _gh_var(repo, "delete")
                else:
                    _gh_var(repo, "set", prior_override)
            # Branch delete may be policy-blocked (unattended sessions park
            # branch deletes) -- best-effort, report either way.
            result = _run(
                ["git", "-C", str(clone), "push", "origin", "--delete", rehearse_ref],
                timeout=60,
            )
            if result.returncode == 0:
                print(
                    f"Cleaned up: PR closed, override restored, {rehearse_ref} deleted"
                )
            else:
                print(
                    f"PR closed + override restored; branch {rehearse_ref} NOT "
                    f"deleted ({result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'unknown'}) "
                    "— delete manually"
                )

    if verdict == "pass":
        print(f"REHEARSAL PASSED: {branch} is safe against {repo}")
        return 0
    if verdict == "timeout":
        print(
            f"REHEARSAL INCONCLUSIVE: no verdict on {branch} against {repo} -- "
            "read the fixture's PR run by hand before merging"
        )
        return 2
    print(f"REHEARSAL FAILED: {branch} broke {repo} — fix before merging to main")
    return 1


if __name__ == "__main__":
    sys.exit(main())
