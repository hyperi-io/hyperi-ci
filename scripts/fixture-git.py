#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/fixture-git.py
# Purpose:   Safe, portable git wrapper scoped to ci-test-* fixture repos
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Run git against a ci-test-* fixture through one allow-listed entry point.

Why this exists
---------------
Unattended runs (hyperi-ai AFK) and the E2E harness do a lot of git work on
the ci-test-* fixture repos - stage, commit, branch, push throwaway
branches. A bare `git ...` is approval-prompted on every call, which stalls
an unattended run and buries the operator in confirmations. This wrapper is
allow-listed ONCE in .claude/settings.local.json; every fixture git op then
runs without a prompt, through a single audited, scope-checked path.

Safety boundary
---------------
ANY git command is allowed against a fixture - force-push, reset --hard,
rebase, clean, the lot. A fixture is a throwaway, re-clonable test repo, so
there is nothing to protect INSIDE it. The one and only invariant is the
scope: the wrapper touches a git repo whose directory name starts with
`ci-test-` and NOTHING else. hyperi-ci itself and any non-fixture path are
refused, and scope-escaping flags (-C / --git-dir / --work-tree) are refused
because they would redirect git AWAY from the validated fixture - so the
allow-list entry can never be turned against a real repo.

Portability
-----------
No hardcoded paths. A fixture is resolved from, in order:
  1. a path (absolute or relative to CWD) that exists, or
  2. a bare name (`ci-test-go-app`) under the fixtures root:
     $HYPERCI_FIXTURES_DIR, else the parent dir of this checkout, else the
     current working directory's parent.

Usage
-----
    fixture-git.py <repo> <git-args...>
    fixture-git.py ci-test-go-app status --short
    fixture-git.py ../ci-test-rust-lib commit -m "fix: drop deprecated releaserc"
    fixture-git.py --list
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIXTURE_PREFIX = "ci-test-"

# Flags that would re-target git away from the validated fixture. These are
# the only refused tokens: not to restrict fixture git, but to keep the
# wrapper pointed at the fixture it validated (and nowhere else).
_SCOPE_ESCAPE = {"-C", "--git-dir", "--work-tree"}


def fixtures_root() -> Path:
    """Directory the fixture checkouts live under.

    $HYPERCI_FIXTURES_DIR wins; otherwise the parent of this checkout
    (this file is <repo>/scripts/fixture-git.py, so parents[2] is the dir
    that holds both <repo> and the ci-test-* siblings).
    """
    env = os.environ.get("HYPERCI_FIXTURES_DIR")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2]


def resolve_fixture(name_or_path: str) -> Path:
    """Resolve a fixture given either a path or a bare `ci-test-*` name."""
    candidate = Path(name_or_path).expanduser()
    if candidate.exists():
        return candidate.resolve()
    return (fixtures_root() / name_or_path).resolve()


def is_fixture(path: Path) -> bool:
    """True iff `path` is a git repo whose name is in the fixture namespace."""
    return path.name.startswith(FIXTURE_PREFIX) and (path / ".git").exists()


def forbidden_reason(git_args: list[str]) -> str | None:
    """Return why `git_args` is refused, or None if it is allowed.

    The only refusals are scope ones: empty args, or a flag that would
    redirect git away from the validated fixture. Every actual git
    operation on the fixture (force-push, reset --hard, clean, rebase, ...)
    is allowed - a fixture is throwaway. Pure, so the policy is testable.
    """
    if not git_args:
        return "no git subcommand given"
    for arg in git_args:
        if arg in _SCOPE_ESCAPE or arg.startswith(("--git-dir=", "--work-tree=")):
            return f"{arg} would escape the fixture scope - refused"
    return None


def list_fixtures() -> list[Path]:
    """Every `ci-test-*` git repo under the fixtures root."""
    root = fixtures_root()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and is_fixture(p))


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] in ("--list", "-l"):
        for path in list_fixtures():
            print(path)
        return 0
    if len(argv) < 2:
        print("usage: fixture-git.py <repo> <git-args...>", file=sys.stderr)
        return 2

    repo = resolve_fixture(argv[0])
    git_args = argv[1:]

    if not is_fixture(repo):
        print(
            f"refused: {repo} is not a {FIXTURE_PREFIX}* fixture git repo "
            "(that namespace is the wrapper's entire scope)",
            file=sys.stderr,
        )
        return 3
    reason = forbidden_reason(git_args)
    if reason:
        print(f"refused: {reason}", file=sys.stderr)
        return 3

    proc = subprocess.run(
        ["git", "-C", str(repo), *git_args],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
