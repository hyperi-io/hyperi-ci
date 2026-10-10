# Project:   HyperI CI
# File:      src/hyperi_ci/release_commit.py
# Purpose:   Commit the rendered release artefacts back, without tagging them
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Put the rendered VERSION and CHANGELOG back on the branch after a release.

The tag is created first, at the real commit, by ``tag-head`` or by
semantic-release. This runs afterwards and only adds an untagged commit, so no
tag points at machine-authored history (the cause of issue #37).

Written through the GitHub Git Data API rather than ``git push``, because the
Tag-and-Publish checkout sets ``persist-credentials: false`` and has no push
credentials.

GitHub rejects a non-fast-forward ref update, so a branch that moved retries
against the new tip instead of overwriting a push. A refused update on a branch
that did not move is a ruleset or permission refusal and fails at once.
"""

import base64
import binascii
import os
import random
import time
from pathlib import Path
from urllib.parse import quote

from packaging.version import InvalidVersion, Version

from hyperi_ci import release_prepare
from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.config import load_config
from hyperi_ci.gh import gh_api
from hyperi_ci.release_branches import is_prerelease_version, repo_prerelease_branches
from hyperi_ci.release_notify import refusal_advice
from hyperi_ci.stamp import (
    CHANGELOG_FILE,
    SUPPLEMENT_FILE,
    VERSION_FILE,
    carried_stamp_paths,
)

# Rendered outputs: VERSION from `stamp-version`, CHANGELOG.md from
# @semantic-release/changelog. A repo adds its own via `release.stamp_paths`.
VERSION = VERSION_FILE
CHANGELOG = CHANGELOG_FILE
RELEASE_ARTEFACTS = (VERSION, CHANGELOG)

# Hand-written notes for the version being cut, printed by
# @semantic-release/exec and removed in the same commit.
SUPPLEMENT = SUPPLEMENT_FILE

# `[skip ci]` stops the push retriggering a CI run with no `Release: true`
# trailer.
_MESSAGE = "chore(release): v{version} [skip ci]"

# A merge burst outlasts a few immediate retries, so each one waits longer,
# jittered so two releases racing the same branch do not retry in step.
_RETRIES = 6
_BACKOFF_CAP_SECONDS = 30.0

# gh's stderr from the most recent `gh api` call, "" when it had none, quoted
# in refusal reports.
_last_api_error: str = ""


def _api(args: list[str], *, body: dict | None = None) -> dict | None:
    """Call `gh api`, returning the parsed response or None on failure.

    Records this call's stderr in :data:`_last_api_error`, clearing it when
    there is none, so a refusal never quotes an earlier call's error.
    """
    global _last_api_error
    outcome = gh_api(args, body=body)
    _last_api_error = outcome.stderr or ""
    return outcome.data


def _stamped_artefacts(root: Path) -> list[str]:
    """Return the ``release.stamp_paths`` files that are safe to commit.

    A broken ``stamp_paths`` is reported and dropped, so VERSION and
    CHANGELOG.md still land.
    """
    listed = carried_stamp_paths(
        load_config(project_dir=root, reload=True), root, who="release-commit"
    )
    kept: list[str] = []
    for name in listed:
        path = root / name
        if path.is_symlink():
            warn(f"release-commit: release.stamp_paths entry {name} is a symlink")
        elif not path.is_file():
            warn(f"release-commit: release.stamp_paths entry {name} is not a file")
        else:
            kept.append(name)
    return kept


def _restore_prepared(root: Path, version: str) -> bool:
    """Bring the stamp outputs over from ``release-prepare``, when a run split it.

    The publish job runs no stamp of its own because ``release.stamp_cmd`` is
    repo code (issue #409). The ``stamp_paths`` files come from the prepare job,
    only when it ran on this same commit. VERSION is written here from the
    release version, and only where git tracks it.

    Returns:
        False when the prepared directory is set but unusable.

    """
    ok, prepared = release_prepare.load_or_report("release-commit")
    if not ok:
        return False
    if prepared is None:
        return True
    if release_prepare.tracked(root, VERSION):
        (root / VERSION).write_text(f"{version}\n", encoding="utf-8", newline="\n")
    here = release_prepare.head_commit(root)
    if prepared.head and here and prepared.head != here:
        warn(
            f"release-commit: prepare ran on {prepared.head[:8]} but this checkout "
            f"is {here[:8]} -- leaving release.stamp_paths out"
        )
        return True
    names = carried_stamp_paths(
        load_config(project_dir=root, reload=True), root, who="release-commit"
    )
    restored = release_prepare.restore_stamped(prepared, root, names)
    info(f"release-commit: restored {', '.join(restored) or 'nothing'} from prepare")
    return True


def _local_blob(root: Path, name: str) -> str | None:
    """Return the blob sha of ``name`` in the checkout's HEAD, if it has one."""
    result = run_cmd(
        ["git", "rev-parse", "--verify", "--quiet", f"HEAD:{name}"],
        capture=True,
        check=False,
        cwd=root,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _unchanged_on_tip(
    *, repo: str, root: Path, tip: str, names: list[str]
) -> list[str]:
    """Keep the stamped files the branch has not changed since this checkout.

    The checkout can be older than the tip (a merge landed mid-release, or a
    retroactive dispatch checked out an old tag), and writing its copy over a
    newer one would lose that change.
    """
    kept: list[str] = []
    for name in names:
        local = _local_blob(root, name)
        remote = _api([f"repos/{repo}/contents/{quote(name)}?ref={tip}"])
        remote_sha = (remote or {}).get("sha")
        if local == remote_sha:
            kept.append(name)
        else:
            warn(
                f"release-commit: {name} changed on the branch since this "
                "checkout -- leaving the branch's copy alone"
            )
    return kept


def _released(repo: str, version: str) -> bool:
    """Say whether ``v<version>`` is a tag, so a VERSION naming it was released.

    ``version`` is the file's own text: packaging normalises ``1.3.0-beta.1``
    to ``1.3.0b1``, which names no tag. Only a 404 answers no: an API failure
    keeps the branch's copy.
    """
    if _api([f"repos/{repo}/git/ref/tags/v{version}"]) is not None:
        return True
    return "404" not in _last_api_error


def _tip_is_newer(*, repo: str, root: Path, tip: str) -> bool:
    """Say whether the branch tip carries a later VERSION than this checkout.

    A retroactive dispatch or a forced bump below the latest tag stamps an older
    version, and committing it would move the branch backwards. A side that is
    missing or unparseable skips the check, and so does a tip VERSION no release
    tag names, which is left over from a release that never published.
    """
    local = root / VERSION
    if not local.is_file():
        return False
    remote = _api([f"repos/{repo}/contents/{VERSION}?ref={tip}"])
    encoded = (remote or {}).get("content")
    if not encoded:
        return False
    try:
        ours = Version(local.read_text(encoding="utf-8", errors="replace").strip())
        raw = base64.b64decode(encoded).decode("utf-8", errors="replace").strip()
        theirs = Version(raw)
    except (InvalidVersion, binascii.Error):
        return False
    if ours >= theirs:
        return False
    if not _released(repo, raw):
        warn(
            f"release-commit: the branch's VERSION says {raw}, but no "
            f"v{raw} tag exists, so it was never released -- replacing it "
            f"with {ours}"
        )
        return False
    info(
        f"release-commit: the branch already carries v{theirs}, newer than "
        f"v{ours} on disk -- committing nothing"
    )
    return True


def _blob_entries(
    repo: str, root: Path, artefacts: list[str]
) -> list[dict[str, str | None]] | None:
    """Upload each artefact as a blob, returning tree entries by sha.

    Content goes up base64-encoded so non-UTF-8 bytes and CRs survive.
    """
    entries: list[dict[str, str | None]] = []
    for name in artefacts:
        path = root / name
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        blob = _api(
            ["-X", "POST", f"repos/{repo}/git/blobs"],
            body={"content": encoded, "encoding": "base64"},
        )
        if not blob or "sha" not in blob:
            error(f"release-commit: failed to upload {name} as a blob")
            return None
        mode = "100755" if path.stat().st_mode & 0o111 else "100644"
        entries.append({"path": name, "mode": mode, "type": "blob", "sha": blob["sha"]})
    return entries


def _supplement_entry(
    *, repo: str, root: Path, branch: str, version: str
) -> list[dict[str, str | None]]:
    """Return the tree entry that deletes a consumed supplement, if any.

    A null sha removes a path in the tree API. The rendered changelog must name
    the version, because a forced bump skips semantic-release and prints the
    supplement into nothing. The branch must still carry the file, because the
    API rejects deleting a path the base tree lacks.
    """
    if not (root / SUPPLEMENT).is_file():
        return []
    changelog = root / CHANGELOG
    rendered = (
        changelog.read_text(encoding="utf-8", errors="replace")
        if changelog.is_file()
        else ""
    )
    if version not in rendered:
        info(f"release-commit: no v{version} entry in {CHANGELOG} -- {SUPPLEMENT} kept")
        return []
    if _api([f"repos/{repo}/contents/{SUPPLEMENT}?ref={branch}"]) is None:
        info(f"release-commit: {SUPPLEMENT} is not on {branch} -- leaving it alone")
        return []
    return [{"path": SUPPLEMENT, "mode": "100644", "type": "blob", "sha": None}]


def commit_release_artefacts(
    *,
    version: str,
    branch: str = "main",
    project_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Commit the rendered release artefacts onto ``branch``, untagged.

    The same commit deletes ``.github/release-notes/NEXT.md`` when the
    release consumed one.

    A prerelease version is refused on any branch the release config does not
    declare ``prerelease``, so a beta release cannot put its VERSION and
    CHANGELOG entry onto ``main`` (issue #417).

    Args:
        version: Version just released, used in the commit subject.
        branch: Branch to update. Defaults to ``main``. The release workflow
            passes the branch it released from.
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

    if is_prerelease_version(version) and branch not in repo_prerelease_branches(root):
        error(
            f"release-commit: v{version} is a prerelease and {branch} is not a "
            "prerelease branch in the release config -- committing nothing. "
            "Pass --branch with the branch the release was cut from."
        )
        return 1

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        error("release-commit: GITHUB_REPOSITORY not set (must run in CI)")
        return 1

    if not _restore_prepared(root, version):
        return 1

    fixed = [name for name in RELEASE_ARTEFACTS if (root / name).is_file()]
    stamped = _stamped_artefacts(root)
    present = fixed + stamped
    consumed = [SUPPLEMENT] if (root / SUPPLEMENT).is_file() else []
    if not present and not consumed:
        info("release-commit: no release artefacts on disk -- nothing to commit")
        return 0

    if dry_run:
        plan = f"commit {', '.join(present)}" if present else "commit nothing"
        if consumed:
            plan += f" and remove {SUPPLEMENT}"
        info(f"release-commit: would {plan} on {branch}")
        return 0

    for attempt in range(1, _RETRIES + 1):
        outcome = _attempt(
            repo=repo,
            root=root,
            version=version,
            branch=branch,
            fixed=fixed,
            stamped=stamped,
        )
        if outcome != "retry":
            return 0 if outcome == "ok" else 1
        if attempt == _RETRIES:
            break
        delay = _backoff(attempt)
        warn(
            f"release-commit: {branch} moved while committing "
            f"(attempt {attempt}/{_RETRIES}) -- rebuilding on the new tip "
            f"in {delay:.1f}s"
        )
        time.sleep(delay)

    error(f"release-commit: {branch} kept moving -- giving up after {_RETRIES} tries")
    return 1


def _backoff(attempt: int) -> float:
    """Return the wait before retry ``attempt + 1``: doubling, capped, jittered."""
    ceiling = min(2.0**attempt, _BACKOFF_CAP_SECONDS)
    return random.uniform(ceiling / 2, ceiling)  # noqa: S311 - jitter, not a secret


def _attempt(
    *,
    repo: str,
    root: Path,
    version: str,
    branch: str,
    fixed: list[str],
    stamped: list[str],
) -> str:
    """One create-tree/commit/update-ref cycle. Returns ok, retry or fail."""
    ref = _api([f"repos/{repo}/git/ref/heads/{branch}"])
    tip = (ref or {}).get("object", {}).get("sha")
    if not tip:
        error(f"release-commit: cannot read {branch} ref")
        return "fail"

    if _tip_is_newer(repo=repo, root=root, tip=tip):
        return "ok"

    head = _api([f"repos/{repo}/git/commits/{tip}"])
    base_tree = (head or {}).get("tree", {}).get("sha")
    if not base_tree:
        error(f"release-commit: cannot read the tree of {tip[:8]}")
        return "fail"

    fresh = _unchanged_on_tip(repo=repo, root=root, tip=tip, names=stamped)
    entries = _blob_entries(repo, root, fixed + fresh)
    if entries is None:
        return "fail"
    entries += _supplement_entry(repo=repo, root=root, branch=branch, version=version)
    if not entries:
        info("release-commit: nothing to write")
        return "ok"

    tree = _api(
        ["-X", "POST", f"repos/{repo}/git/trees"],
        body={"base_tree": base_tree, "tree": entries},
    )
    new_tree = (tree or {}).get("sha")
    if not new_tree:
        error("release-commit: cannot create the tree")
        return "fail"

    # An identical tree would make an empty commit.
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

    # No `force`: a non-fast-forward rejection turns a concurrent push into a retry.
    updated = _api(
        ["-X", "PATCH", f"repos/{repo}/git/refs/heads/{branch}"],
        body={"sha": new_commit, "force": False},
    )
    if not updated:
        return _classify_refused_update(repo=repo, branch=branch, tip=tip)

    success(
        f"release-commit: {branch} now carries v{version} ({new_commit[:8]}, untagged)"
    )
    return "ok"


def _classify_refused_update(*, repo: str, branch: str, tip: str) -> str:
    """Tell a branch that moved (retry) from a push GitHub refused (fail).

    A refused update whose branch still points at ``tip`` was a ruleset or
    permission refusal, which a retry cannot change.
    """
    reason = _last_api_error
    ref = _api([f"repos/{repo}/git/ref/heads/{branch}"])
    now = (ref or {}).get("object", {}).get("sha")
    if now and now != tip:
        return "retry"
    error(
        f"release-commit: GitHub refused the update to {branch}, which has not "
        f"moved -- a ruleset or the token's permissions blocked the push"
        + (f": {reason}" if reason else "")
    )
    error(f"release-commit: {refusal_advice()}")
    return "fail"
