# Project:   HyperI CI
# File:      src/hyperi_ci/release_commit.py
# Purpose:   Commit the rendered release artefacts back, without tagging them
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Put the rendered VERSION and CHANGELOG back on the branch after a release.

``@semantic-release/git`` used to do this and was dropped in May 2026: it
created the release tag ON ITS OWN BOT COMMIT, so a later history rewrite
orphaned the tag and the next release recomputed a version that already existed
(issue #37). Both files then stopped moving in every repo.

The tag is created first, at the real commit, by ``tag-head`` or by
semantic-release. This runs afterwards and only ever adds an untagged commit,
so no tag can ever point at machine-authored history — the property whose
absence caused #37.

Written through the GitHub Git Data API rather than ``git push``: the
Tag-and-Publish checkout sets ``persist-credentials: false`` and has no push
credentials, the same constraint ``tag-head`` works around.

Concurrency is handled by the ref update itself. GitHub rejects a non-fast-
forward ref update, so a branch that moved under us fails loudly and retries
against the new tip instead of overwriting someone's push.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn

# The rendered artefacts. Both are outputs: VERSION is written by
# `stamp-version`, CHANGELOG.md by @semantic-release/changelog.
RELEASE_ARTEFACTS = ("VERSION", "CHANGELOG.md")

# `[skip ci]` keeps the commit from triggering another run. Without it the
# push retriggers CI, which finds no `Publish: true` trailer and validates
# for nothing.
_MESSAGE = "chore(release): v{version} [skip ci]"

_RETRIES = 3


def _api(args: list[str], *, body: dict | None = None) -> dict | None:
    """Call `gh api`, returning the parsed response or None on failure."""
    tmp_path: str | None = None
    cmd = ["gh", "api", *args]
    if body is not None:
        # A tree is an array of objects, which `-f key=value` cannot express.
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


def _blob_entries(repo: str, root: Path) -> list[dict[str, str]] | None:
    """Upload each artefact as a blob, returning tree entries by sha.

    Content goes up base64-encoded so a file that is not valid UTF-8 (or
    carries a stray CR) survives the round trip intact.
    """
    entries: list[dict[str, str]] = []
    for name in RELEASE_ARTEFACTS:
        path = root / name
        if not path.is_file():
            continue
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        blob = _api(
            ["-X", "POST", f"repos/{repo}/git/blobs"],
            body={"content": encoded, "encoding": "base64"},
        )
        if not blob or "sha" not in blob:
            error(f"release-commit: failed to upload {name} as a blob")
            return None
        entries.append(
            {"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]}
        )
    return entries


def commit_release_artefacts(
    *,
    version: str,
    branch: str = "main",
    project_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Commit the rendered release artefacts onto ``branch``, untagged.

    Args:
        version: Version just released, used in the commit subject.
        branch: Branch to update. Defaults to ``main``.
        project_dir: Project root. Defaults to cwd.
        dry_run: Report what would be committed, change nothing.

    Returns:
        0 when the branch ends up carrying the artefacts (committed, or
        already identical), 1 on a failure worth surfacing.

    """
    root = project_dir or Path.cwd()
    version = version.removeprefix("v").strip()
    if not version:
        error("release-commit: empty version")
        return 1

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        error("release-commit: GITHUB_REPOSITORY not set (must run in CI)")
        return 1

    present = [name for name in RELEASE_ARTEFACTS if (root / name).is_file()]
    if not present:
        info("release-commit: no release artefacts on disk — nothing to commit")
        return 0

    if dry_run:
        info(f"release-commit: would commit {', '.join(present)} to {branch}")
        return 0

    for attempt in range(1, _RETRIES + 1):
        outcome = _attempt(repo=repo, root=root, version=version, branch=branch)
        if outcome != "retry":
            return 0 if outcome == "ok" else 1
        warn(
            f"release-commit: {branch} moved while committing "
            f"(attempt {attempt}/{_RETRIES}) — rebuilding on the new tip"
        )

    error(f"release-commit: {branch} kept moving — giving up after {_RETRIES} tries")
    return 1


def _attempt(*, repo: str, root: Path, version: str, branch: str) -> str:
    """One create-tree/commit/update-ref cycle. Returns ok, retry or fail."""
    ref = _api([f"repos/{repo}/git/ref/heads/{branch}"])
    tip = (ref or {}).get("object", {}).get("sha")
    if not tip:
        error(f"release-commit: cannot read {branch} ref")
        return "fail"

    head = _api([f"repos/{repo}/git/commits/{tip}"])
    base_tree = (head or {}).get("tree", {}).get("sha")
    if not base_tree:
        error(f"release-commit: cannot read the tree of {tip[:8]}")
        return "fail"

    entries = _blob_entries(repo, root)
    if entries is None:
        return "fail"

    tree = _api(
        ["-X", "POST", f"repos/{repo}/git/trees"],
        body={"base_tree": base_tree, "tree": entries},
    )
    new_tree = (tree or {}).get("sha")
    if not new_tree:
        error("release-commit: cannot create the tree")
        return "fail"

    # An identical tree means the artefacts on disk already match the branch —
    # a re-run, or a release that changed neither file. Committing would add an
    # empty commit for nothing.
    if new_tree == base_tree:
        info(f"release-commit: {branch} already matches the rendered artefacts")
        return "ok"

    commit = _api(
        ["-X", "POST", f"repos/{repo}/git/commits"],
        body={
            "message": _MESSAGE.format(version=version),
            "tree": new_tree,
            "parents": [tip],
        },
    )
    new_commit = (commit or {}).get("sha")
    if not new_commit:
        error("release-commit: cannot create the commit")
        return "fail"

    # No `force`: GitHub rejects a non-fast-forward update, which is what makes
    # a concurrent push a retry rather than a silent overwrite.
    updated = _api(
        ["-X", "PATCH", f"repos/{repo}/git/refs/heads/{branch}"],
        body={"sha": new_commit, "force": False},
    )
    if not updated:
        return "retry"

    success(
        f"release-commit: {branch} now carries v{version} ({new_commit[:8]}, untagged)"
    )
    return "ok"
