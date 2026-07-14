#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/rehearse-branch.py
# Purpose:   Rehearse a hyperi-ci branch against a real fixture repo
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
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
4. pushes the branch and opens a DRAFT pull request — the pull_request
   run exercises the branch's workflows + CLI through quality / test /
   build / container (a dev push lands in the prunable ``branch-*``
   namespace on an opted-in fixture),
5. watches the run, reports per-job outcomes, then cleans up (closes the
   PR, clears the variable, best-effort deletes the branch).

Deliberately NEVER: merges anything, touches the fixture's main, or
publishes. Known limit (accepted, pinning decision #31 gate-only):
composite refs INSIDE the lang workflows stay @main — composite changes
are covered by hyperi-ci's own local-ref ci.yml instead.

Usage:
    uv run scripts/rehearse-branch.py --branch fix/my-change \
        --repo hyperi-io/ci-test-go-app [--keep] [--no-cli-override] \
        [--timeout-minutes 20]
"""

from __future__ import annotations

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

    Returns (new_text, swap_count). Pure function — unit-tested.
    """
    new_text, count = _REF_SWAP.subn(rf"\1@{branch}", text)
    return new_text, count


def rehearse_slug(branch: str) -> str:
    """Git-ref-safe slug of the candidate branch name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-.")[:80]


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def _branch_exists(branch: str) -> bool:
    result = _run(
        ["gh", "api", f"/repos/{_HYPERI_CI_REPO}/branches/{branch}", "--jq", ".name"]
    )
    return result.returncode == 0


def _gh_var(repo: str, action: str, value: str = "") -> bool:
    """Set or delete HYPERCI_INSTALL_OVERRIDE on the fixture. True on success."""
    if action == "set":
        cmd = [
            "gh",
            "variable",
            "set",
            "HYPERCI_INSTALL_OVERRIDE",
            "--body",
            value,
            "-R",
            repo,
        ]
    else:
        cmd = ["gh", "variable", "delete", "HYPERCI_INSTALL_OVERRIDE", "-R", repo]
    result = _run(cmd)
    if result.returncode != 0 and action == "set":
        print(result.stderr, file=sys.stderr)
    return result.returncode == 0


def _watch_pr_run(
    repo: str, pr_number: int, timeout_minutes: int
) -> tuple[bool, list[str]]:
    """Poll the PR's checks until all complete. Returns (all_green, lines)."""
    deadline = time.time() + timeout_minutes * 60
    lines: list[str] = []
    while time.time() < deadline:
        result = _run(
            [
                "gh",
                "pr",
                "checks",
                str(pr_number),
                "-R",
                repo,
                "--json",
                "name,state",
            ],
            timeout=60,
        )
        if result.returncode not in (0, 8):  # 8 = checks still pending
            time.sleep(20)
            continue
        try:
            checks = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            checks = []
        if checks and all(
            c.get("state") not in ("PENDING", "QUEUED", "IN_PROGRESS", "")
            for c in checks
        ):
            lines = [f"  {c.get('state'):<9} {c.get('name')}" for c in checks]
            ok = all(
                c.get("state") in ("SUCCESS", "SKIPPED", "NEUTRAL") for c in checks
            )
            return ok, lines
        time.sleep(30)
    return False, ["  TIMEOUT waiting for PR checks"]


def main() -> int:
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
    if not _branch_exists(branch):
        return _fail(
            f"branch {branch!r} not found on {_HYPERI_CI_REPO} — push it first"
        )

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
        if not args.no_cli_override:
            value = (
                "uvx --from "
                f"git+https://github.com/{_HYPERI_CI_REPO}@{branch} hyperi-ci"
            )
            override_set = _gh_var(repo, "set", value)
            if not override_set:
                return _fail("could not set HYPERCI_INSTALL_OVERRIDE")
            print(f"HYPERCI_INSTALL_OVERRIDE set on {repo}")

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
                "Throwaway rehearsal PR created by scripts/rehearse-branch.py. "
                f"Exercises hyperi-ci@{branch} workflows"
                + ("" if args.no_cli_override else " + branch CLI")
                + " against this fixture. Never merged; cleaned up automatically.",
            ],
            timeout=60,
        )
        if result.returncode != 0:
            return _fail(f"PR create failed: {result.stderr.strip()}")
        pr_url = result.stdout.strip()
        pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        print(f"Rehearsal PR: {pr_url}")

        ok, lines = _watch_pr_run(repo, pr_number, args.timeout_minutes)
        print("Rehearsal run results:")
        for line in lines:
            print(line)

        if args.keep:
            print("--keep: leaving PR, branch, and override in place")
        else:
            _run(["gh", "pr", "close", str(pr_number), "-R", repo], timeout=60)
            if override_set:
                _gh_var(repo, "delete")
            # Branch delete may be policy-blocked (unattended sessions park
            # branch deletes) — best-effort, report either way.
            result = _run(
                ["git", "-C", str(clone), "push", "origin", "--delete", rehearse_ref],
                timeout=60,
            )
            if result.returncode == 0:
                print(
                    f"Cleaned up: PR closed, override cleared, {rehearse_ref} deleted"
                )
            else:
                print(
                    f"PR closed + override cleared; branch {rehearse_ref} NOT "
                    f"deleted ({result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'unknown'}) "
                    "— delete manually"
                )

    if ok:
        print(f"REHEARSAL PASSED: {branch} is safe against {repo}")
        return 0
    print(f"REHEARSAL FAILED: {branch} broke {repo} — fix before merging to main")
    return 1


if __name__ == "__main__":
    sys.exit(main())
