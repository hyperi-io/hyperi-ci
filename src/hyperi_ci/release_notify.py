# Project:   HyperI CI
# File:      src/hyperi_ci/release_notify.py
# Purpose:   Tell someone a release shipped, or that it died
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Close the loop after a release, the way semantic-release's github plugin does.

Its ``success`` step comments on every issue and PR carried by a release, and
its ``fail`` step opens an issue when a release breaks. That plugin is one of
the two we deliberately never load (issue #37), so both behaviours were simply
lost: a failed release was visible only as a red run somebody had to notice.

Two notifications, both idempotent because a re-run is normal:

* **success** -- one comment per issue or PR referenced by the commits in the
  release, saying which version carries it.
* **failure** -- one open issue per broken version, so a release that dies at
  3am is waiting in the tracker rather than buried in a run log.

Slack is a third channel, off unless ``notify.slack.webhook_env`` names an
environment variable holding a webhook URL. Nothing is posted off-org by
default -- an outbound notification is a decision for whoever owns the channel.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig

# `#123`, but not a colour literal (`#abc`) or a trailing digit of a word.
_ISSUE_REF = re.compile(r"(?:^|[\s(\[,])#(\d+)\b")

# Marks our own comments so a re-run recognises them. Invisible when rendered.
_MARKER = "<!-- hyperi-ci:release-notify -->"

_FAILURE_TITLE = "Release v{version} failed"


def _api(args: list[str], *, body: dict | None = None) -> dict | list | None:
    """Call `gh api`, returning the parsed response or None on failure."""
    tmp_path: str | None = None
    cmd = ["gh", "api", *args]
    if body is not None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8", newline="\n"
        ) as handle:
            json.dump(body, handle)
            tmp_path = handle.name
        cmd += ["--input", tmp_path]
    try:
        result = run_cmd(cmd, capture=True, check=False)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def _previous_tag(version: str, *, cwd: str | None = None) -> str | None:
    """Find the tag before ``v{version}``, which bounds the release's commits."""
    result = run_cmd(
        ["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    current = f"v{version}"
    if current in tags:
        after = tags[tags.index(current) + 1 :]
        return after[0] if after else None
    return tags[0] if tags else None


def referenced_issues(version: str, *, cwd: str | None = None) -> list[int]:
    """Issue and PR numbers referenced by the commits in this release.

    Reads the commit range since the previous tag. A merge commit's
    ``(#123)`` suffix covers the squash-merge case, which is how most work
    lands here.
    """
    previous = _previous_tag(version, cwd=cwd)
    span = f"{previous}..v{version}" if previous else f"v{version}"
    result = run_cmd(
        ["git", "log", "--format=%B", span], capture=True, check=False, cwd=cwd
    )
    if result.returncode != 0:
        return []
    numbers = {int(match) for match in _ISSUE_REF.findall(result.stdout)}
    return sorted(numbers)


def _already_commented(repo: str, number: int, version: str) -> bool:
    """Check whether a previous run already announced this version here."""
    comments = _api([f"repos/{repo}/issues/{number}/comments", "--paginate"])
    if not isinstance(comments, list):
        return False
    return any(
        _MARKER in str(comment.get("body", ""))
        and f"v{version}" in str(comment.get("body", ""))
        for comment in comments
    )


def notify_success(
    *, version: str, repo: str | None = None, cwd: str | None = None
) -> int:
    """Comment on every issue and PR carried by this release.

    Returns:
        0 always -- a notification that fails must never fail a release that
        already shipped.

    """
    version = version.removeprefix("v").strip()
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        warn("release-notify: GITHUB_REPOSITORY not set — skipping")
        return 0

    numbers = referenced_issues(version, cwd=cwd)
    if not numbers:
        info(f"release-notify: no issues or PRs referenced by v{version}")
        return 0

    body = (
        f"{_MARKER}\nReleased in **v{version}** — "
        f"https://github.com/{repo}/releases/tag/v{version}"
    )
    posted = 0
    for number in numbers:
        if _already_commented(repo, number, version):
            continue
        if _api(
            ["-X", "POST", f"repos/{repo}/issues/{number}/comments"],
            body={"body": body},
        ):
            posted += 1
        else:
            # A #123 in a commit message may be a reference to another repo,
            # or an issue since deleted.
            info(f"release-notify: could not comment on #{number} — skipping")
    success(f"release-notify: announced v{version} on {posted} issue(s)/PR(s)")
    return 0


def _open_failure_issue(repo: str, version: str, run_url: str) -> int | None:
    """Existing open failure issue for this version, or a newly created one."""
    title = _FAILURE_TITLE.format(version=version)
    found = _api(
        [
            "-X",
            "GET",
            f"repos/{repo}/issues",
            "-f",
            "state=open",
            "-f",
            "labels=release-failure",
        ]
    )
    if isinstance(found, list):
        for issue in found:
            if str(issue.get("title", "")) == title:
                return int(issue["number"])

    body = (
        f"{_MARKER}\n"
        f"The release of **v{version}** failed.\n\n"
        f"- Run: {run_url or 'see the Actions tab'}\n"
        f"- The tag and the registry artefact may disagree — check both before "
        f"re-running.\n\n"
        f"Retry with `hyperi-ci publish --version {version}` once the cause is "
        f"fixed, or `hyperi-ci publish --bump patch` to ship past it."
    )
    created = _api(
        ["-X", "POST", f"repos/{repo}/issues"],
        body={"title": title, "body": body, "labels": ["release-failure"]},
    )
    if isinstance(created, dict) and "number" in created:
        return int(created["number"])
    return None


def notify_failure(*, version: str, repo: str | None = None, run_url: str = "") -> int:
    """Open (or reuse) a tracker issue for a release that broke.

    Returns:
        0 always -- the release already failed; this must not add a second
        failure on top of it.

    """
    version = version.removeprefix("v").strip()
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or not version:
        warn("release-notify: repository or version unknown — skipping")
        return 0

    number = _open_failure_issue(repo, version, run_url)
    if number is None:
        error(f"release-notify: could not record the v{version} failure as an issue")
        return 0
    success(f"release-notify: v{version} failure tracked in #{number}")
    return 0


def notify_slack(config: CIConfig, *, text: str) -> int:
    """Post to Slack, if a webhook has been configured for this project.

    The webhook lives in an environment variable named by
    ``notify.slack.webhook_env``; the URL itself is a secret and never appears
    in config. Unset means no Slack, which is the default.
    """
    variable = str(config.get("notify.slack.webhook_env", "") or "")
    if not variable:
        return 0
    webhook = os.environ.get(variable, "")
    if not webhook:
        warn(f"release-notify: {variable} names no webhook — skipping Slack")
        return 0

    result = run_cmd(
        [
            "curl",
            "-sS",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"text": text}),
            webhook,
        ],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        warn("release-notify: Slack post failed")
        return 0
    success("release-notify: posted to Slack")
    return 0
