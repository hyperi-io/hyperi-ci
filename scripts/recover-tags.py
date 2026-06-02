#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/recover-tags.py
# Purpose:   Rebuild v* tags destroyed by the issue #37 tag-rewrite bug
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Recover release tags rewritten off-main by the #37 central-tagger bug.

A legacy `.releaserc` carrying `@semantic-release/git` rewrote every `v*`
tag on affected consumers (dfe-receiver et al.) to point at fresh off-main
`chore: version X.Y.Z [skip ci]` commits, so the tags no longer reach
`main`. The ORIGINAL release commits are still on `main` — each version's
`chore: version X.Y.Z` commit. This rebuilds `vX.Y.Z -> that commit`.

Safe by construction:
  - default is a DRY RUN: prints the plan, mutates nothing.
  - --apply moves/creates tags LOCALLY only and prints the exact
    `git push --force` command for you to run by hand. This script never
    pushes — the remote mutation stays a deliberate human step.

Usage:
    uv run scripts/recover-tags.py --repo /path/to/clone           # dry run
    uv run scripts/recover-tags.py --repo /path/to/clone --apply   # local tags + print push cmd

Recovery flow per repo:
    git clone <repo> && cd <clone>
    git fetch origin "+refs/tags/*:refs/tags/*" --force
    uv run scripts/recover-tags.py --repo . --apply
    # review the printed plan + push command, then run it with explicit consent
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

# semantic-release / @semantic-release/git release-commit subjects, covering
# both the modern `chore: version X.Y.Z` and the older `chore(release): X.Y.Z`.
_CHORE_RE = re.compile(
    r"^chore(?:\(release\))?:\s*(?:version\s*)?(\d+\.\d+\.\d+)(?=\s|$)",
)


def _git(repo: str, *args: str) -> str:
    """Run a git command in `repo`, return stripped stdout (raise on error)."""
    out = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _git_ok(repo: str, *args: str) -> bool:
    """Run a git command, return True iff exit 0 (for existence probes)."""
    return (
        subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )


@dataclass
class Plan:
    """One version's recovery decision."""

    version: str
    target: str  # commit the tag SHOULD point at (oldest chore commit)
    current: str | None  # what the tag points at now, or None if missing
    action: str  # OK | CREATE | MOVE
    ambiguous: bool  # >1 chore commit for this version


def _release_commits(
    repo: str, branch: str, source_ref: str = ""
) -> dict[str, list[str]]:
    """Map each version -> chore commits reachable from the source ref(s).

    Default (source_ref="") is branch-model-agnostic: scans commits reachable
    from `origin/*`, so it works for both main-only and release-branch repos.
    Pass an explicit `source_ref` (e.g. "origin/main") to restrict originals to
    that line — "only recover tags whose original is on current main". Either
    way this EXCLUDES the bug's orphaned bot commits (off every branch). `branch`
    is the fallback when no remote refs are present (local-only test repo).
    """
    if source_ref:
        spec = [source_ref]
    elif _git(repo, "for-each-ref", "refs/remotes/origin").strip():
        spec = ["--remotes=origin"]
    else:
        spec = [branch]  # fallback: no remote refs, use the named branch
    # newest-first from git log; reverse so index 0 is the original (oldest).
    log = _git(repo, "log", *spec, "--format=%H%x09%s")
    found: dict[str, list[str]] = {}
    for line in reversed(log.splitlines()):
        sha, _, subject = line.partition("\t")
        m = _CHORE_RE.match(subject.strip())
        if m:
            found.setdefault(m.group(1), []).append(sha)
    return found


def build_plans(repo: str, branch: str, source_ref: str = "") -> list[Plan]:
    """Compute the recovery plan for every recoverable version."""
    commits = _release_commits(repo, branch, source_ref)
    plans: list[Plan] = []
    for version, shas in sorted(
        commits.items(),
        key=lambda kv: tuple(int(p) for p in kv[0].split(".")),
    ):
        target = shas[0]  # oldest chore commit = the original release point
        tag = f"v{version}"
        current: str | None = None
        if _git_ok(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"):
            current = _git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}")
        if current is None:
            action = "CREATE"
        elif current == target:
            action = "OK"
        elif _git(repo, "branch", "-r", "--contains", current).strip():
            # Current target is itself ON a branch — a valid commit, not the
            # bug's orphaned bot commit. Never clobber a healthy tag; only #37
            # damage (tag pointing OFF every branch) is repaired.
            action = "KEEP"
        else:
            action = "MOVE"
        plans.append(
            Plan(
                version=version,
                target=target,
                current=current,
                action=action,
                ambiguous=len(shas) > 1,
            )
        )
    return plans


def _print_report(plans: list[Plan]) -> None:
    """Print actionable rows (MOVE/CREATE); OK/KEEP are summarised elsewhere."""
    actionable = [p for p in plans if p.action in ("MOVE", "CREATE")]
    if not actionable:
        print("No tags need repair — all dangling tags either point at a valid")
        print("commit (KEEP) or are already correct (OK).")
        return
    print(f"{'TAG':<14} {'ACTION':<7} {'TARGET':<10} CURRENT")
    for p in actionable:
        cur = p.current[:9] if p.current else "(missing)"
        flag = "  [AMBIGUOUS: multiple chore commits]" if p.ambiguous else ""
        print(f"v{p.version:<13} {p.action:<7} {p.target[:9]:<10} {cur}{flag}")


def _unrecoverable_tags(repo: str, branch: str, planned: set[str]) -> list[str]:
    """v* tags that have no `chore: version` source AND don't reach branch.

    These cannot be rebuilt automatically (no original commit to map to) and
    must be handled by hand — surfaced so nothing is silently dropped.
    """
    all_tags = [t for t in _git(repo, "tag", "-l", "v*").splitlines() if t]
    out = []
    for t in all_tags:
        if t[1:] in planned:
            continue
        # reachable from any remote branch -> legitimate, leave alone.
        on_branch = _git(repo, "branch", "-r", "--contains", t).strip()
        if not on_branch:
            out.append(t)
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", help="path to the affected clone")
    ap.add_argument("--branch", default="main", help="branch carrying release commits")
    ap.add_argument(
        "--source-ref",
        default="",
        help="restrict originals to this ref (e.g. origin/main); default scans all origin branches",
    )
    ap.add_argument(
        "--no-create",
        action="store_true",
        help="only repoint existing tags (MOVE); never CREATE a missing tag",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="create/move tags LOCALLY and print the push command (never pushes)",
    )
    args = ap.parse_args()

    if not _git_ok(args.repo, "rev-parse", "--git-dir"):
        print(f"error: {args.repo} is not a git repository", file=sys.stderr)
        return 2

    plans = build_plans(args.repo, args.branch, args.source_ref)
    if args.no_create:
        plans = [p for p in plans if p.action != "CREATE"]
    if not plans:
        print(
            f"No 'chore: version X.Y.Z' release commits on {args.branch} — "
            "nothing to recover (repo may never have used @semantic-release/git).",
        )
        return 0

    _print_report(plans)

    todo = [p for p in plans if p.action in ("CREATE", "MOVE")]
    ambiguous = [p for p in plans if p.ambiguous]
    print()
    print(
        f"{len(plans)} versions, {len(todo)} to fix "
        f"({sum(p.action == 'MOVE' for p in plans)} MOVE, "
        f"{sum(p.action == 'CREATE' for p in plans)} CREATE), "
        f"{sum(p.action == 'KEEP' for p in plans)} KEEP (valid, untouched), "
        f"{sum(p.action == 'OK' for p in plans)} OK, "
        f"{len(ambiguous)} ambiguous."
    )
    if ambiguous:
        print(
            "AMBIGUOUS versions have >1 chore commit (e.g. a bogus one from a "
            "failed publish). The OLDEST was chosen as the original; review "
            "these before pushing.",
        )

    unrecoverable = _unrecoverable_tags(
        args.repo, args.branch, {p.version for p in plans}
    )
    if unrecoverable:
        print(
            f"\n{len(unrecoverable)} dangling tag(s) have NO 'chore: version' "
            "source commit and cannot be auto-rebuilt — handle by hand:",
        )
        print("    " + " ".join(unrecoverable))

    if not args.apply:
        print("\nDry run — nothing changed. Re-run with --apply to set local tags.")
        return 0

    for p in todo:
        _git(args.repo, "tag", "-f", f"v{p.version}", p.target)
    print(f"\nSet {len(todo)} tags locally.")
    if todo:
        tags = " ".join(f"v{p.version}" for p in todo)
        print(
            "\nReview, then push by hand (destructive, force) with explicit consent:\n"
            f"    git -C {args.repo} push --force origin {tags}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
