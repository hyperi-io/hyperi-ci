#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/release-branch-guard/branch_guard.py
# Purpose:   Stop a release before it publishes once its branch has moved
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Fail when the release branch on GitHub no longer points at ``GITHUB_SHA``.

semantic-release tags nothing once its checkout is behind the branch, so a run
that publishes first and tags second leaves artefacts with no tag behind them
(issue #717). This runs before each publishing step instead.

A re-run after the tag was cut passes when ``v<version>`` already tags this
commit: semantic-release then has nothing to do and the upload completes the
release that tag names.

Stdlib only, with no annotation newer than the runner's python3 can read: the
composite runs before anything is installed. An unreadable tip fails closed,
because publishing past an unchecked branch is the failure this exists to stop.
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_ATTEMPTS = 3


def _get(url: str, token: str) -> tuple:
    """GET ``url`` from the GitHub API and return ``(status, body)``.

    A 404 returns at once. A transport failure or a 5xx is retried, and after
    the last attempt the status is 0 with no body.
    """
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for attempt in range(_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - the runner's own GITHUB_API_URL
                raw = response.read().decode("utf-8", errors="replace")
            return 200, json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return exc.code, None
            reason = f"HTTP {exc.code}"
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            ValueError,
        ) as exc:
            reason = str(exc) or type(exc).__name__
        print(f"GET {url} failed (attempt {attempt + 1}): {reason}", file=sys.stderr)
        if attempt + 1 < _ATTEMPTS:
            time.sleep(2 * (attempt + 1))
    return 0, None


def _object_sha(body: object) -> tuple:
    """Return ``(type, sha)`` of a git ref or tag object body, empty when absent."""
    obj = body.get("object") if isinstance(body, dict) else None
    if not isinstance(obj, dict):
        return "", ""
    return str(obj.get("type", "")), str(obj.get("sha", ""))


def tip_of(api: str, repo: str, branch: str, token: str) -> str:
    """Return the commit ``branch`` points at on GitHub, or "" when unreadable."""
    quoted = urllib.parse.quote(branch, safe="/")
    _, body = _get(f"{api}/repos/{repo}/git/ref/heads/{quoted}", token)
    return _object_sha(body)[1]


def tag_commit(api: str, repo: str, tag: str, token: str) -> str:
    """Return the commit ``tag`` names on GitHub, or "" when absent or unreadable.

    An annotated tag is dereferenced once, as the release workflow's tag guard does.
    """
    quoted = urllib.parse.quote(tag, safe="")
    _, body = _get(f"{api}/repos/{repo}/git/ref/tags/{quoted}", token)
    kind, sha = _object_sha(body)
    if kind != "tag" or not sha:
        return sha
    _, annotated = _get(f"{api}/repos/{repo}/git/tags/{sha}", token)
    return _object_sha(annotated)[1]


def verdict(
    branch: str,
    sha: str,
    tip: str,
    tagged: str,
    version: str,
    before: str,
    image: str,
) -> tuple:
    """Return ``(exit code, message)`` for a release of ``sha`` on ``branch``.

    ``tip`` is the branch's commit on GitHub and ``tagged`` the commit
    ``v<version>`` names there, each "" when unknown.
    """
    if tip == sha:
        return 0, f"{branch} is still at {sha}."
    if version and tagged == sha:
        return 0, (
            f"v{version} already tags {sha}, so this re-run completes that release "
            f"although {branch} has moved."
        )
    recover = (
        "Release the current head as a new version with 'hyperi-ci release', or "
        "re-publish a version that is already tagged with "
        f"'hyperi-ci release v{version or 'X.Y.Z'}'."
    )
    if not tip:
        return 1, (
            f"::error::Could not read {branch} on GitHub, so this run stops before "
            f"{before}. Re-run the job once GitHub answers. {recover}"
        )
    pushed = (
        f" The image {image} was already pushed, so it has no tag behind it."
        if image
        else ""
    )
    return 1, (
        f"::error::{branch} moved past {sha} (now {tip}) while this release ran, so "
        f"this run stops before {before}.{pushed} {recover}"
    )


def main() -> int:
    """Check the branch named by the composite's environment and print the verdict."""
    branch = os.environ.get("RELEASE_BRANCH", "")
    sha = os.environ.get("RELEASE_SHA", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not (branch and sha and repo):
        print(
            "::error::release-branch-guard needs RELEASE_BRANCH, RELEASE_SHA and GITHUB_REPOSITORY."
        )
        return 1
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    token = os.environ.get("GH_TOKEN", "")
    version = os.environ.get("RELEASE_VERSION", "")
    tip = tip_of(api, repo, branch, token)
    tagged = (
        tag_commit(api, repo, f"v{version}", token) if version and tip != sha else ""
    )
    code, message = verdict(
        branch,
        sha,
        tip,
        tagged,
        version,
        os.environ.get("GUARD_BEFORE", "") or "publishing",
        os.environ.get("PUSHED_IMAGE", ""),
    )
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
