#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/runner_mode.py
# Purpose:   Resolve the run's runner mode from the caller, the org and repo visibility
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Write ``runner-mode`` (``free`` or ``self-hosted``) to ``$GITHUB_OUTPUT``.

An explicit mode wins: the caller's ``runner-mode`` input, then the
``GH_RUNNER_MODE`` variable, both passed in as ``RUNNER_MODE``. With neither,
a public repository runs free and a private or internal one self-hosted.
Visibility comes from the event payload, else the repositories API, because a
schedule event's payload is not documented to carry the repository. A
repository whose visibility cannot be read runs self-hosted, which is never
billed.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs this composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

FREE = "free"
SELF_HOSTED = "self-hosted"
_API = "https://api.github.com"


def _visibility_from_api(repo: str, token: str) -> str:
    request = urllib.request.Request(
        f"{_API}/repos/{repo}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            visibility = json.load(response).get("visibility")
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    return visibility if isinstance(visibility, str) else ""


def resolve(explicit: str, payload_visibility: str, lookup) -> tuple[str, str]:
    """Return the mode and the reason, for the log.

    Args:
        explicit: The caller's input or GH_RUNNER_MODE, empty when unset.
        payload_visibility: ``github.event.repository.visibility``, empty when
            the payload has no repository.
        lookup: Called with no arguments for the API's visibility when the
            payload is silent. Returns it, or empty when it cannot be read.

    Returns:
        ``(mode, reason)``.

    """
    if explicit:
        mode = FREE if explicit == FREE else SELF_HOSTED
        return mode, f"runner-mode or GH_RUNNER_MODE is {explicit!r}"
    visibility, source = payload_visibility, "the event payload"
    if not visibility:
        visibility, source = lookup(), "the repositories API"
    if not visibility:
        return SELF_HOSTED, "repository visibility unknown, so self-hosted"
    if visibility == "public":
        return FREE, f"public repository ({source})"
    return SELF_HOSTED, f"{visibility} repository ({source})"


def run() -> int:
    """Resolve the mode and write the output. Never fails the step."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    mode, reason = resolve(
        os.environ.get("RUNNER_MODE", "").strip(),
        os.environ.get("REPO_VISIBILITY", "").strip().lower(),
        lambda: _visibility_from_api(repo, token) if repo and token else "",
    )
    line = f"runner-mode={mode}\n"
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
    else:
        sys.stdout.write(line)
    print(f"::notice title=runner mode::{mode}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
