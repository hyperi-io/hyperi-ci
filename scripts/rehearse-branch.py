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


def _read_override(repo: str) -> str | None:
    """The fixture's current HYPERCI_INSTALL_OVERRIDE, or None when unset."""
    result = _run(
        [
            "gh",
            "api",
            f"/repos/{repo}/actions/variables/{_OVERRIDE_VAR}",
            "--jq",
            ".value",
        ]
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


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


def _watch_pr_run(
    repo: str, rehearse_ref: str, timeout_minutes: int
) -> tuple[str, list[str], int]:
    """Wait for the rehearsal's pull_request run and read it job by job.

    Uses `gh run list` / `gh run view`, which every supported gh has, rather
    than `gh pr checks --json`, which older gh rejects outright.

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
                "databaseId,status",
                "--limit",
                "1",
            ],
            timeout=60,
        )
        runs: list[dict] = []
        if listed.returncode == 0:
            try:
                runs = json.loads(listed.stdout or "[]")
            except json.JSONDecodeError:
                last_error = "unreadable gh run list output"
        else:
            stderr_lines = listed.stderr.strip().splitlines()
            last_error = (
                stderr_lines[-1] if stderr_lines else f"gh exited {listed.returncode}"
            )
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
                wf.write_text(new_text, encoding="utf-8")
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

        override_set = False
        # A fixture may already carry a permanent override (ci-test-manifests
        # pins @main), so cleanup restores this rather than deleting the key.
        prior_override = _read_override(repo)
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

        verdict, lines, run_id = _watch_pr_run(repo, rehearse_ref, args.timeout_minutes)
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
