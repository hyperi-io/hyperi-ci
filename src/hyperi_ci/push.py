# Project:   HyperI CI
# File:      src/hyperi_ci/push.py
# Purpose:   Push wrapper with pre-checks and meta-operations
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Push wrapper with pre-checks and meta-operations.

Wraps git push with:

- Pre-push validation (``hyperi-ci check``)
- Auto-rebase to sync semantic-release commits
- ``--publish`` (alias ``--release``): amend HEAD with the ``Release: true``
  git trailer before pushing, so the one CI run produces the tag + registry
  uploads
- ``--no-ci``: amend last commit with ``[skip ci]`` marker

All flows set ``HYPERCI_PUSH=1`` so the pre-push hook allows the push.
"""

import os
import subprocess
from pathlib import Path

from hyperi_ci.common import (
    env_true,
    error,
    explicit_version,
    info,
    run_cmd,
    success,
    warn,
)
from hyperi_ci.gh import get_current_branch, require_gh
from hyperi_ci.release_branches import repo_prerelease_branches
from hyperi_ci.version_source import seed_version
from hyperi_ci.vocabulary import (
    TRAILER_KEY,
    TRAILER_VALUE,
    has_release_trailer,
)

RELEASE_TRAILER_KEY = TRAILER_KEY
RELEASE_TRAILER_VALUE = TRAILER_VALUE

# Lockfiles staged into the release-marker commit when modified at commit time.
# Cargo, uv, npm and the rest refresh them during the quality stage, and that
# drift would fail the subsequent rebase.
_AUTO_STAGE_LOCKFILES: frozenset[str] = frozenset(
    {
        "Cargo.lock",
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "go.sum",
    }
)

# Bump -> the conventional-commits type semantic-release reads as that bump.
# "major" is excluded: it needs a human-written `BREAKING CHANGE:` footer.
_BUMP_TO_TYPE: dict[str, str] = {
    "patch": "fix",
    "minor": "feat",
}


def push(
    *,
    publish: bool = False,
    no_ci: bool = False,
    bump: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    project_dir: Path | None = None,
) -> int:
    """Push with pre-checks and optional meta-operations.

    Args:
        publish: Stamp the head commit with the ``Release: true`` trailer and
            push. The CI run predicts the next version, stamps it before
            build, then tags + publishes in the same run.
        no_ci: Amend last commit with ``[skip ci]`` and push.
        bump: ``"patch"`` or ``"minor"``. Adds a release-worthy marker commit
            on top of HEAD when the actual commits are no-bump (e.g. ``docs:``
            only). Implies ``--publish``. Major is excluded: it needs a
            human-written ``BREAKING CHANGE:`` footer.
        dry_run: Show what would happen without executing.
        force: Skip hyperi-ci check step.
        project_dir: Project directory (default: cwd).

    Returns:
        Exit code: 0=success, non-zero=failure.

    """
    if publish and no_ci:
        error("--publish and --no-ci are mutually exclusive")
        return 1
    if bump and no_ci:
        error("--bump-* and --no-ci are mutually exclusive")
        return 1
    if bump and bump not in _BUMP_TO_TYPE:
        error(
            f"Unknown bump level {bump!r}. Use 'patch' or 'minor'. "
            f"Major bumps require a human-written BREAKING CHANGE: footer."
        )
        return 1

    cwd = str(project_dir) if project_dir else None

    if no_ci:
        return _skip_ci_push(dry_run=dry_run, cwd=cwd)

    if publish or bump:
        return _publish_push(dry_run=dry_run, force=force, bump=bump, cwd=cwd)

    return _default_push(dry_run=dry_run, force=force, cwd=cwd)


def _default_push(*, dry_run: bool, force: bool, cwd: str | None) -> int:
    """Check, rebase, push."""
    if rc := _check_dirty_tree(cwd=cwd):
        return rc

    # Before the gates: the answer cannot change while they run.
    if rc := _check_push_target(cwd=cwd):
        return rc

    if not force:
        if rc := _run_check(cwd=cwd):
            return rc

    if dry_run:
        info("Dry run: would rebase and push")
        return 0

    return _rebase_and_push(cwd=cwd)


def _publish_push(
    *,
    dry_run: bool,
    force: bool,
    bump: str | None,
    cwd: str | None,
) -> int:
    """Mark HEAD as a publish run, then push.

    Runs from ``main``, or from a branch the release config declares
    ``prerelease``, which cuts ``1.2.0-beta.1`` on its own sequence (issue
    #144). Any other branch is refused, matching the CI gate.

    Two paths:

    - Default (``bump=None``): HEAD is release-worthy (a ``fix:`` / ``feat:`` /
      ``perf:`` commit), so it is amended with the ``Release: true`` trailer.
    - Forced bump (``bump="patch"`` or ``"minor"``): HEAD is not
      release-worthy (e.g. docs-only), so a marker commit with a patch/minor
      conventional-commit subject and the trailer goes on top. History states
      the forced release instead of hiding a fake fix in source.

    Either way the CI run goes predict -> stamp -> build -> tag + publish.
    """
    if not require_gh():
        return 1

    branch = get_current_branch(cwd=cwd)
    if branch != "main" and not _is_prerelease_branch(branch, cwd=cwd):
        error(
            "--publish only works from main, or from a branch declared "
            "'prerelease' in the release config"
        )
        return 1

    if rc := _check_dirty_tree(cwd=cwd):
        return rc

    # Before the gates: the answer cannot change while they run.
    if rc := _check_push_target(cwd=cwd):
        return rc

    if not force:
        if rc := _run_check(cwd=cwd):
            return rc

    # Dry runs report the verdict on the LOCAL range (issue #26). The
    # authoritative gate for a real push runs after the pull-rebase below,
    # because the pull can import feat!/BREAKING history the local range lacks.
    if dry_run:
        if rc := _bump_gate(cwd=cwd, forced_bump=bump):
            return rc

    if bump:
        # The marker also writes the next version to VERSION, so the commit is
        # non-empty and consumer `paths-ignore` filters do not skip CI for it.
        commit_type = _BUMP_TO_TYPE[bump]
        marker_subject = f"{commit_type}(release): force {bump} bump"

        next_version = _compute_next_version(bump=bump, cwd=cwd)
        if next_version is None:
            error(
                f"Cannot compute next {bump} version -- the starting version "
                f"is not plain X.Y.Z. Check the latest v* tag, or the version "
                f"declared in pyproject.toml / Cargo.toml / package.json."
            )
            return 1

        marker_message = (
            f"{marker_subject} v{next_version}\n\n"
            f"Forced {bump} release requested via `hyperi-ci push --bump-{bump}`.\n"
            f"The preceding commits don't independently warrant a {bump} bump\n"
            f"under conventional-commits rules; this marker commit records\n"
            f"the operator's explicit decision to publish anyway.\n"
            f"\n"
            f"{RELEASE_TRAILER_KEY}: {RELEASE_TRAILER_VALUE}\n"
        )
        if dry_run:
            info(
                f"Dry run: would write VERSION={next_version}, commit "
                f"`{marker_subject} v{next_version}`, then push"
            )
            return 0
        rc = _write_version_and_commit(
            next_version=next_version, message=marker_message, cwd=cwd
        )
        if rc != 0:
            return rc
        info(
            f"Added release-marker: `{marker_subject} v{next_version}` "
            f"(VERSION updated)"
        )
    else:
        head_msg = _get_last_commit_message(cwd=cwd)
        if not head_msg:
            error("Could not read HEAD commit message")
            return 1

        if _has_publish_trailer(head_msg):
            info("HEAD already carries the release trailer -- pushing as-is")
        else:
            if dry_run:
                info(
                    "Dry run: would amend HEAD to add 'Release: true' trailer, "
                    "then push"
                )
                return 0
            rc = _amend_publish_trailer(cwd=cwd)
            if rc != 0:
                return rc

    if dry_run:
        info("Dry run: would rebase and push")
        return 0

    # Sync before the gate so the analysis covers what the push makes
    # reachable: a reconcile merge on origin/main would otherwise ship
    # un-analysed (issue #26). Rebase onto the branch being pushed, as
    # rebasing a prerelease branch onto main would import commits the release
    # was not cut from.
    if rc := _pull_rebase(branch=branch, cwd=cwd):
        return rc

    # Fail closed if the range implies a bump above patch the operator has not
    # authorised (issue #26). A forced --bump-minor already authorises minor.
    if rc := _bump_gate(cwd=cwd, forced_bump=bump):
        return rc

    rc = _push_with_env(cwd=cwd)
    if rc != 0:
        return rc

    info("Pushed. The CI run will tag + publish in a single workflow.")
    info("Watch: hyperi-ci watch")
    return 0


def _emit_gh_output(**pairs: str) -> None:
    """Append ``key=value`` lines to ``$GITHUB_OUTPUT`` when set (CI only)."""
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a", encoding="utf-8", newline="\n") as fh:
        for key, value in pairs.items():
            fh.write(f"{key}={value}\n")


def tag_head(*, bump: str, dry_run: bool = False, cwd: str | None = None) -> int:
    """CI-internal: create the tag at HEAD for a forced release (issue #35).

    ``bump`` is ``patch`` / ``minor`` (``last-tag + bump``) or an explicit
    ``X.Y.Z`` (the ``hyperi-ci publish --version`` override, used verbatim to
    skip a taken or orphaned tag, as in issue #37).

    Creates the tag at HEAD through the API and writes ``version=`` + ``tag=`` to
    ``$GITHUB_OUTPUT`` for the publish step. Used by the from-head dispatch path
    when ``bump != auto`` (``auto`` lets semantic-release pick). Idempotent: an
    existing tag at HEAD is reused.
    """
    explicit = explicit_version(bump)
    if explicit is None and bump not in ("patch", "minor"):
        error(f"tag-head: invalid bump '{bump}' (expected patch, minor, or X.Y.Z)")
        return 1

    next_version = explicit or _compute_next_version(bump=bump, cwd=cwd)
    if not next_version:
        error("tag-head: cannot compute next version (starting version is not X.Y.Z).")
        return 1
    tag = f"v{next_version}"

    if dry_run:
        info(f"tag-head: would create {tag} at HEAD (bump={bump})")
        _emit_gh_output(version=next_version, tag=tag)
        return 0

    head = run_cmd(["git", "rev-parse", "HEAD"], capture=True, check=False, cwd=cwd)
    if head.returncode != 0 or not head.stdout.strip():
        error("tag-head: cannot resolve HEAD sha")
        return 1
    sha = head.stdout.strip()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        error("tag-head: GITHUB_REPOSITORY not set (must run in CI)")
        return 1

    # Never tag over a tag pointing off-HEAD (an orphaned tag, or a typo): that
    # would publish a fresh artefact under a tag at old history (issue #37).
    # Only an explicit version can collide. Tags are local here (fetch-depth: 0),
    # and the gh-api create below is the authoritative check.
    if explicit is not None:
        peeled = run_cmd(
            ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
            capture=True,
            check=False,
            cwd=cwd,
        )
        if peeled.returncode == 0 and peeled.stdout.strip():
            existing_sha = peeled.stdout.strip()
            if existing_sha != sha:
                error(
                    f"tag-head: {tag} already exists at {existing_sha[:8]} but "
                    f"HEAD is {sha[:8]} -- refusing to publish over it (issue #37). "
                    "Pick a free version with --version, or use --bump patch."
                )
                return 1
            _emit_gh_output(version=next_version, tag=tag)
            success(f"tag-head: {tag} already at HEAD ({sha[:8]}) -- nothing to tag.")
            return 0

    # The API with GITHUB_TOKEN, as the checkout sets persist-credentials: false.
    # An existing ref (HTTP 422) is fine: the goal is "tag exists".
    created = run_cmd(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/git/refs",
            "-f",
            f"ref=refs/tags/{tag}",
            "-f",
            f"sha={sha}",
        ],
        capture=True,
        check=False,
        cwd=cwd,
    )
    blob = (created.stdout + created.stderr).lower()
    if created.returncode != 0 and "already exists" not in blob:
        error(f"tag-head: failed to create tag {tag}: {created.stderr.strip()}")
        return created.returncode

    _emit_gh_output(version=next_version, tag=tag)
    success(f"tag-head: {tag} created at HEAD ({sha[:8]})")
    return 0


def _compute_next_version(*, bump: str, cwd: str | None) -> str | None:
    """Compute the next semver string given a bump level.

    Increments the latest ``v*`` tag and returns the bare version. With no
    tags it bumps from the version the project declares in its manifest
    (:func:`version_source.seed_version`). On a tag-less repo the auto path ships
    that seed verbatim, while a forced bump goes FROM it.

    The ``VERSION`` file is not consulted (issue #85): this tool writes it, and
    the committed copy is stale.

    Returns ``None`` only when the starting version is unparseable.
    """
    result = run_cmd(
        ["git", "tag", "--list", "v*", "--sort=-v:refname"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    latest: str | None = None
    if result.returncode == 0 and result.stdout.strip():
        # Plain vX.Y.Z only -- a prerelease sorts above its own release.
        for line in result.stdout.splitlines():
            candidate = explicit_version(line)
            if candidate:
                latest = candidate
                break

    if not latest:
        cwd_path = Path(cwd) if cwd else Path.cwd()
        latest, source = seed_version(cwd_path)
        info(f"No release tags -- bumping from {latest} ({source})")

    parts = latest.split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None

    if bump == "patch":
        patch += 1
    elif bump == "minor":
        minor += 1
        patch = 0
    else:
        # "major" is deliberately unsupported.
        return None

    return f"{major}.{minor}.{patch}"


def _write_version_and_commit(
    *, next_version: str, message: str, cwd: str | None
) -> int:
    """Write VERSION + commit with the given message.

    For ``--bump-patch`` / ``--bump-minor``. The VERSION write makes the commit
    non-empty (consumer ``paths-ignore`` filters), and the ``fix(release):`` /
    ``feat(release):`` subject gives semantic-release the right bump.
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()
    version_file = cwd_path / "VERSION"

    try:
        version_file.write_text(f"{next_version}\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        error(f"Failed to write {version_file}: {exc}")
        return 1

    try:
        run_cmd(["git", "add", "VERSION"], cwd=cwd, capture=True)
        _stage_modified_lockfiles(cwd=cwd)
        run_cmd(
            [
                "git",
                "commit",
                # The marker is a publish trigger, and a re-run leaves no diff
                # (a plain commit exits 1, issue #36).
                "--allow-empty",
                "-m",
                message,
            ],
            cwd=cwd,
            capture=True,
        )
    except subprocess.CalledProcessError as exc:
        error(f"Failed to create release-marker commit: {exc}")
        return 1
    return 0


def _stage_modified_lockfiles(*, cwd: str | None) -> None:
    """Stage any modified lockfiles (Cargo.lock, uv.lock, ...) into the marker.

    Quality / test runs refresh lockfiles to match the manifest, and left
    unstaged they make the next ``git pull --rebase`` fail on "unstaged
    changes". Only basenames in :data:`_AUTO_STAGE_LOCKFILES` are staged.
    """
    result = run_cmd(
        ["git", "diff", "--name-only"],
        cwd=cwd,
        capture=True,
    )
    modified = (result.stdout or "").splitlines()
    for rel_path in modified:
        rel_path = rel_path.strip()
        if not rel_path:
            continue
        if Path(rel_path).name in _AUTO_STAGE_LOCKFILES:
            run_cmd(["git", "add", rel_path], cwd=cwd, capture=True)


def _has_publish_trailer(message: str) -> bool:
    """Return True if the message carries the release trailer, either spelling.

    The matcher is in :mod:`hyperi_ci.vocabulary`, shared with the predict-version
    composite's shell check.
    """
    return has_release_trailer(message)


def _amend_publish_trailer(*, cwd: str | None) -> int:
    """Amend HEAD to add the Release: true trailer (no message change)."""
    # --allow-empty: git refuses to amend an already-empty HEAD (an empty
    # `chore: trigger` marker) without it.
    try:
        run_cmd(
            [
                "git",
                "commit",
                "--amend",
                "--no-edit",
                "--allow-empty",
                "--trailer",
                f"{RELEASE_TRAILER_KEY}: {RELEASE_TRAILER_VALUE}",
            ],
            cwd=cwd,
            capture=True,
        )
    except subprocess.CalledProcessError as exc:
        error(f"Failed to amend HEAD with Release: true trailer: {exc}")
        return 1
    info(f"Amended HEAD with `{RELEASE_TRAILER_KEY}: {RELEASE_TRAILER_VALUE}` trailer")
    return 0


def _skip_ci_push(*, dry_run: bool, cwd: str | None) -> int:
    """Amend last commit with [skip ci], push with --force-with-lease."""
    if rc := _check_dirty_tree(cwd=cwd):
        return rc

    if rc := _check_not_ci_commit(cwd=cwd):
        return rc

    msg = _get_last_commit_message(cwd=cwd)
    if not msg:
        error("Could not read last commit message")
        return 1

    if "[skip ci]" in msg:
        warn("Last commit already contains [skip ci]")
        if dry_run:
            return 0
        return _push_with_env(args=["--force-with-lease"], cwd=cwd)

    new_msg = f"{msg} [skip ci]"

    if dry_run:
        info(f"Dry run: would amend commit message to: {new_msg}")
        return 0

    try:
        run_cmd(
            ["git", "commit", "--amend", "-m", new_msg],
            cwd=cwd,
            capture=True,
        )
    except subprocess.CalledProcessError:
        error("Failed to amend commit")
        return 1

    info("Amended commit with [skip ci]")
    return _push_with_env(args=["--force-with-lease"], cwd=cwd)


# --- helpers ---


def _is_prerelease_branch(branch: str | None, *, cwd: str | None) -> bool:
    """Report whether ``branch`` is declared a prerelease branch for this repo.

    The CI gate reads the same declaration.

    Args:
        branch: Current branch name, or None when it cannot be resolved.
        cwd: Repository root, or None for the process working directory.

    Returns:
        True when the branch releases on its own version sequence.

    """
    if not branch:
        return False
    workspace = Path(cwd) if cwd else Path.cwd()
    return branch in repo_prerelease_branches(workspace)


def _check_dirty_tree(*, cwd: str | None) -> int:
    """Check for uncommitted changes. Returns 0 if clean, 1 if dirty."""
    result = run_cmd(
        ["git", "status", "--porcelain"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if result.stdout.strip():
        error("Uncommitted changes. Commit or stash first.")
        return 1
    return 0


# `push.default` values that need the upstream's name to match the branch's.
# `simple` is git's default, so unset counts too.
_NAME_MATCHED_PUSH = frozenset({"", "simple"})


def _check_push_target(*, cwd: str | None) -> int:
    """Refuse now what git will refuse after the gates. Returns 0 if OK.

    A branch created in a worktree inherits its parent's upstream, so `fix/x`
    can track `main`, and `push.default=simple` then refuses the push. The
    gates cannot change that, so this runs before them (issue #210).
    """
    branch = get_current_branch(cwd=cwd)
    if not branch:
        return 0

    mode = run_cmd(
        ["git", "config", "--get", "push.default"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if mode.stdout.strip() not in _NAME_MATCHED_PUSH:
        return 0

    # `branch.<name>.merge` names the upstream branch with no remote prefix, so
    # a slash in the branch name is unambiguous.
    tracked = run_cmd(
        ["git", "config", "--get", f"branch.{branch}.merge"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    upstream = tracked.stdout.strip().removeprefix("refs/heads/")
    if not upstream or upstream == branch:
        return 0

    error(
        f"'{branch}' tracks '{upstream}', and push.default=simple refuses a "
        f"push whose upstream is named differently. git would reject this "
        f"after the gates, so it is rejected now."
    )
    info(f"  Point it at its own branch:  git push -u origin {branch}")
    info(f"  Or retarget the upstream:    git branch --set-upstream-to=origin/{branch}")
    return 1


def _check_not_ci_commit(*, cwd: str | None) -> int:
    """Check last commit is not a semantic-release version commit. Returns 0 if OK."""
    msg = _get_last_commit_message(cwd=cwd)
    if not msg:
        return 0

    if msg.startswith("chore: version ") or msg.startswith("chore(release):"):
        error("Cannot amend CI version commit. Make a new commit first.")
        return 1
    return 0


def _get_last_commit_message(*, cwd: str | None) -> str | None:
    """Get the last commit's full message."""
    result = run_cmd(
        ["git", "log", "-1", "--format=%B"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _run_check(*, cwd: str | None) -> int:
    """Run hyperi-ci check. Returns exit code."""
    from hyperi_ci.dispatch import run_stage

    dir_path = Path(cwd) if cwd else None
    for stage in ("quality", "test"):
        rc = run_stage(stage, project_dir=dir_path, local=True)
        if rc != 0:
            return rc
    return 0


_BUMP_RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}


def _bump_gate(*, cwd: str | None, forced_bump: str | None) -> int:
    """Fail closed when the publish would ship an unintended minor/major (#26).

    Compares the predicted bump over ``<last-tag>..HEAD`` with what the operator
    authorised:

    * a predicted MAJOR needs ``HYPERCI_ALLOW_MAJOR_BUMP=1`` (or
      ``HYPERCI_ALLOW_BREAKING=1``);
    * a predicted MINOR needs ``HYPERCI_ALLOW_MINOR_BUMP=1`` (or
      ``HYPERCI_ALLOW_FEAT=1``, or an explicit ``--bump-minor``);
    * PATCH / none always pass.

    The real publish flow runs this after the pull-rebase, so the range includes
    history the pull imported. The dry-run call sees the local range only.

    Fails open (returns 0) when the bump can't be predicted (no prior tag, no
    new commits, or git unavailable), so an initial release is never blocked.
    """
    from hyperi_ci.quality.predicted_bump import predict_bump

    project_dir = Path(cwd) if cwd else None
    prediction = predict_bump(project_dir)

    # An explicit --bump-* pre-authorises that level. The commit-text opt-ins
    # (ALLOW_BREAKING, ALLOW_FEAT) declare the same intent, so they authorise
    # major and minor here too.
    authorised = _BUMP_RANK.get(forced_bump or "none", 0)
    if env_true("HYPERCI_ALLOW_MAJOR_BUMP") or env_true("HYPERCI_ALLOW_BREAKING"):
        authorised = max(authorised, _BUMP_RANK["major"])
    elif env_true("HYPERCI_ALLOW_MINOR_BUMP") or env_true("HYPERCI_ALLOW_FEAT"):
        authorised = max(authorised, _BUMP_RANK["minor"])

    predicted_rank = _BUMP_RANK.get(prediction.bump, 0)
    if predicted_rank <= max(authorised, _BUMP_RANK["patch"]):
        return 0

    reasons = (
        prediction.major_reasons
        if prediction.bump == "major"
        else prediction.minor_reasons
    )
    env_flag = (
        "HYPERCI_ALLOW_MAJOR_BUMP"
        if prediction.bump == "major"
        else "HYPERCI_ALLOW_MINOR_BUMP"
    )
    error(
        f"Predicted release bump is {prediction.bump.upper()} "
        f"(since {prediction.last_tag}) but no such bump was authorised."
    )
    warn(
        "This can happen when a merge / cherry-pick brings already-committed "
        "feat!/BREAKING history into reachability without you authoring it "
        "(issue #26 -- how rustlib shipped an unintended v3.0.0)."
    )
    if reasons:
        info(f"  Commits driving the {prediction.bump} bump:")
        for subject in reasons[:10]:
            info(f"    - {subject}")
        if len(reasons) > 10:
            info(f"    ... and {len(reasons) - 10} more")
    info(
        f"  If this bump IS intended, re-run with {env_flag}=1 set: "
        f"`{env_flag}=1 hyperi-ci push --publish`"
    )
    return 1


def _has_upstream(*, cwd: str | None) -> bool:
    """Return True if the current branch has a configured upstream.

    A never-pushed branch has no ``@{u}``, which marks a first push with nothing
    to rebase against.
    """
    result = run_cmd(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _pull_rebase(*, branch: str | None = None, cwd: str | None = None) -> int:
    """Run ``git pull --rebase`` (optionally against ``origin <branch>``)."""
    rebase_cmd = ["git", "pull", "--rebase"]
    if branch:
        rebase_cmd.extend(["origin", branch])

    try:
        run_cmd(rebase_cmd, cwd=cwd)
    except subprocess.CalledProcessError:
        error("Rebase failed -- resolve conflicts and try again")
        return 1
    return 0


def _rebase_and_push(
    *,
    branch: str | None = None,
    cwd: str | None = None,
) -> int:
    """Pull --rebase then push with HYPERCI_PUSH=1.

    A new branch has no upstream and ``git pull --rebase`` aborts with "no
    tracking information" (issue #38), so the rebase is skipped and the push
    uses ``-u origin <branch>`` to set tracking.
    """
    if not _has_upstream(cwd=cwd):
        current = branch or get_current_branch(cwd=cwd)
        if not current:
            error("Cannot determine current branch to push")
            return 1
        info(f"No upstream for '{current}' -- first push, setting upstream")
        return _push_with_env(args=["-u", "origin", current], cwd=cwd)

    if rc := _pull_rebase(branch=branch, cwd=cwd):
        return rc

    return _push_with_env(cwd=cwd)


def _push_with_env(
    *,
    args: list[str] | None = None,
    cwd: str | None = None,
) -> int:
    """Run git push with HYPERCI_PUSH=1 set."""
    cmd = ["git", "push"]
    if args:
        cmd.extend(args)

    try:
        run_cmd(cmd, env={"HYPERCI_PUSH": "1"}, cwd=cwd)
    except subprocess.CalledProcessError:
        # git's stderr is not captured here (issue #210).
        error("Push failed -- git's reason is in its output directly above.")
        return 1

    success("Pushed successfully")
    return 0
