#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/versions-audit-pr.py
# Purpose:   Keep one rolling pin-bump PR open while versions.yaml has drifted
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Turn the weekly versions audit's bumps into one rolling PR.

`versions-audit.yml` runs `update-versions.py --auto-update` when the audit
finds drift, which rewrites versions.yaml and its mirrors in the working tree.
This script commits that tree to the bot branch `deps/versions-audit`,
force-updates the branch, and opens a PR from it unless one is already open.
An open PR picks up the new commit in place, so there is never more than one.

With no drift on main, an open PR from the branch is stale and is closed.

A bump to a pin mirrored into a composite action's `default:` goes live across
the fleet on merge, because consumers run the composites from `@main`. The PR
body lists those separately.

Usage (CI only -- it commits and pushes):
    uv run scripts/versions-audit-pr.py --drift-outcome success|failure

`--drift-outcome` is the outcome of the `--stable --fail-on-drift` report step.
Exit 0 on success, 1 when a git or gh call fails.
"""

import argparse
import base64
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from hyperi_ci.common import error, info, run_cmd, success

BRANCH = "deps/versions-audit"
COMMIT_SUBJECT = "fix(deps): bump drifted pins (versions-audit)"
VERSIONS_REL = "src/hyperi_ci/config/versions.yaml"
COMPOSITE_GATE = "composite default changes: owner approval needed before merge"
CLOSE_COMMENT = "No pin drift on main any more, so this bump is no longer needed."
_ACTIONS_PREFIX = ".github/actions/"
_BOT_NAME = "github-actions[bot]"
_BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
_ROOT = Path(__file__).resolve().parent.parent

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class Bump:
    """One tool pin the audit moved.

    Attributes:
        name: The `tools:` key in versions.yaml.
        old: Version on main.
        new: Version the bump writes.
        composites: Composite action files the pin is mirrored into.
    """

    name: str
    old: str
    new: str
    composites: tuple[str, ...]


class Action(StrEnum):
    """What the run does to the bot branch and its PR."""

    NOOP = "noop"
    OPEN = "open"
    UPDATE = "update"
    CLOSE = "close"


def _pin_paths(spec: dict) -> list[str]:
    pin = spec.get("pin")
    if not pin:
        return []
    return [pin] if isinstance(pin, str) else [str(p) for p in pin]


def bumps_between(old: dict, new: dict) -> list[Bump]:
    """Every `tools:` entry whose version differs between two SSOT loads."""
    old_tools = old.get("tools") or {}
    out: list[Bump] = []
    for name, spec in (new.get("tools") or {}).items():
        before = old_tools.get(name)
        if not isinstance(spec, dict) or not isinstance(before, dict):
            continue
        if str(before.get("version")) == str(spec.get("version")):
            continue
        composites = tuple(p for p in _pin_paths(spec) if p.startswith(_ACTIONS_PREFIX))
        out.append(
            Bump(name, str(before.get("version")), str(spec.get("version")), composites)
        )
    return out


def render_body(bumps: list[Bump]) -> str:
    """PR body: the bumps, then the composite defaults that need a yes."""
    lines = [
        "The weekly versions audit found these pins behind. Each is the newest"
        " release past the 7-day soak, written by"
        " `scripts/update-versions.py --auto-update`.",
        "",
    ]
    lines += [f"- {b.name} {b.old} -> {b.new}" for b in bumps]
    gated = [b for b in bumps if b.composites]
    if gated:
        lines += [
            "",
            f"**{COMPOSITE_GATE}**",
            "",
            "These pins feed a composite action default, which consumers run"
            " from `@main`, so they go live fleet-wide on merge:",
            "",
        ]
        lines += [f"- {b.name} -> {', '.join(b.composites)}" for b in gated]
    lines += [
        "",
        f"Each audit run force-updates `{BRANCH}`. Do not push to it by hand.",
    ]
    return "\n".join(lines) + "\n"


def decide(drift_clean: bool, bumps: list[Bump], open_prs: list[int]) -> Action:
    """Pick the action for this run.

    Drift that `--auto-update` could not write (a hand bump, an unreadable
    upstream, a failed local gate) leaves no bumps and no clean report, so it
    touches nothing: the open PR, if any, still holds the last good bumps.
    """
    if drift_clean:
        return Action.CLOSE if open_prs else Action.NOOP
    if not bumps:
        return Action.NOOP
    return Action.UPDATE if open_prs else Action.OPEN


def auth_env(token: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Git config env that sends `token` to github.com, kept off argv.

    Appended after any GIT_CONFIG_* entries already set, the same shape the
    release tail uses.
    """
    current = os.environ if environ is None else environ
    n = int(current.get("GIT_CONFIG_COUNT", "0") or "0")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": str(n + 1),
        f"GIT_CONFIG_KEY_{n}": "http.https://github.com/.extraheader",
        f"GIT_CONFIG_VALUE_{n}": f"AUTHORIZATION: basic {basic}",
    }


def open_prs(repo: str, run: Runner = run_cmd) -> list[int]:
    """Numbers of the open PRs from the bot branch, oldest first."""
    result = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            BRANCH,
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            ".[].number",
        ],
        capture=True,
    )
    return sorted(int(n) for n in result.stdout.split())


def commit_and_push(
    bumps: list[Bump], token: str, cwd: Path, run: Runner = run_cmd
) -> None:
    """Commit the bumped tree to the bot branch and force-update only that."""
    body = "\n".join(f"- {b.name} {b.old} -> {b.new}" for b in bumps)
    run(["git", "switch", "-C", BRANCH], cwd=cwd)
    # -u: --auto-update only rewrites tracked files, and `uv run` leaves an
    # untracked .venv behind.
    run(["git", "add", "-u"], cwd=cwd)
    run(
        [
            "git",
            "-c",
            f"user.name={_BOT_NAME}",
            "-c",
            f"user.email={_BOT_EMAIL}",
            "commit",
            "-m",
            COMMIT_SUBJECT,
            "-m",
            body,
        ],
        cwd=cwd,
    )
    run(
        ["git", "push", "--force", "origin", f"HEAD:refs/heads/{BRANCH}"],
        cwd=cwd,
        env=auth_env(token),
    )


def sync(
    *,
    drift_clean: bool,
    repo: str,
    token: str,
    cwd: Path,
    run: Runner = run_cmd,
) -> Action:
    """Bring the bot branch and its PR in line with this run's audit."""
    old = yaml.safe_load(
        run(["git", "show", f"HEAD:{VERSIONS_REL}"], capture=True, cwd=cwd).stdout
    )
    new = yaml.safe_load((cwd / VERSIONS_REL).read_text(encoding="utf-8"))
    bumps = bumps_between(old or {}, new or {})
    prs = open_prs(repo, run)
    action = decide(drift_clean, bumps, prs)
    info(f"versions-audit PR: {action} ({len(bumps)} bump(s), open PRs: {prs})")

    if action is Action.CLOSE:
        for pr in prs:
            run(
                [
                    "gh",
                    "pr",
                    "close",
                    str(pr),
                    "--repo",
                    repo,
                    "--comment",
                    CLOSE_COMMENT,
                    "--delete-branch",
                ]
            )
        return action
    if action is Action.NOOP:
        return action

    commit_and_push(bumps, token, cwd, run)
    body = render_body(bumps)
    if action is Action.OPEN:
        run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                "main",
                "--head",
                BRANCH,
                "--title",
                COMMIT_SUBJECT,
                "--body",
                body,
            ]
        )
        return action
    # The force-push already moved the PR. The body follows, since this week's
    # bumps can differ from last week's.
    keep, *extra = prs
    run(["gh", "pr", "edit", str(keep), "--repo", repo, "--body", body])
    for pr in extra:
        run(
            [
                "gh",
                "pr",
                "close",
                str(pr),
                "--repo",
                repo,
                "--comment",
                f"Superseded by #{keep}.",
            ]
        )
    return action


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--drift-outcome",
        required=True,
        choices=("success", "failure"),
        help="Outcome of the `--stable --fail-on-drift` report step",
    )
    args = parser.parse_args(argv)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "")
    if not repo or not token:
        error("versions-audit PR: needs GITHUB_REPOSITORY and GH_TOKEN")
        return 1
    try:
        action = sync(
            drift_clean=args.drift_outcome == "success",
            repo=repo,
            token=token,
            cwd=_ROOT,
        )
    except subprocess.CalledProcessError as exc:
        error(f"versions-audit PR: {' '.join(exc.cmd[:3])} failed ({exc.returncode})")
        return 1
    success(f"versions-audit PR: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
