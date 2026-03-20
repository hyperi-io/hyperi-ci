# Project:   HyperI CI
# File:      src/hyperi_ci/release_merge.py
# Purpose:   CLI-native release merge (main -> release) with auto conflict resolution
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release merge: merge main into release via gh CLI.

Performs the merge in a temp clone via gh/git, resolves version file
conflicts automatically (keeps release branch versions), and creates a PR.
No workflow file needed in consumer projects.

If gh CLI is not installed or not authenticated, prints the manual
commands the user can run instead.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn

# Files where version conflicts are expected and resolved by keeping
# the release branch's version (semantic-release manages these).
VERSION_FILES = [
    "VERSION",
    "Cargo.toml",
    "pyproject.toml",
    "package.json",
    "CHANGELOG.md",
]


def _gh_available() -> bool:
    """Check gh CLI is installed."""
    return bool(shutil.which("gh"))


def _gh_authenticated() -> bool:
    """Check gh CLI is authenticated."""
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _get_repo_url() -> str | None:
    """Get the remote origin URL from the current directory."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _get_repo_slug() -> str | None:
    """Get owner/repo slug from origin URL."""
    url = _get_repo_url()
    if not url:
        return None
    # Handle both https://github.com/owner/repo.git and git@github.com:owner/repo.git
    url = url.rstrip("/").removesuffix(".git")
    if "github.com" in url:
        parts = url.split("github.com")[-1].lstrip("/:").split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return None


def _print_manual_commands(
    head_branch: str,
    base_branch: str,
) -> None:
    """Print the manual git/gh commands the user can run."""
    merge_branch = f"chore/merge-to-{base_branch}"
    info("")
    info("Run these commands manually:")
    info("")
    info("  # Clone to temp dir and merge")
    info("  cd $(mktemp -d)")
    info("  git clone $(git remote get-url origin) . --no-checkout")
    info(f"  git fetch origin {base_branch} {head_branch}")
    info(f"  git checkout origin/{base_branch}")
    info(f"  git checkout -b {merge_branch}")
    info(f"  git merge origin/{head_branch} --no-edit")
    info("")
    info("  # If conflicts in VERSION/Cargo.toml/CHANGELOG.md:")
    info(
        "  git checkout --ours VERSION Cargo.toml pyproject.toml package.json CHANGELOG.md"
    )
    info("  git add VERSION Cargo.toml pyproject.toml package.json CHANGELOG.md")
    info("  git commit --no-edit")
    info("")
    info("  # Push and create PR")
    info(f"  git push -u origin {merge_branch}")
    info(f"  gh pr create --base {base_branch} --head {merge_branch} \\")
    info(f'    --title "chore: merge {head_branch} into {base_branch}"')
    info("")


def _git(
    args: list[str], cwd: Path | None = None, **kwargs
) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    return run_cmd(["git", *args], cwd=cwd, **kwargs)


def _gh(
    args: list[str], cwd: Path | None = None, **kwargs
) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command."""
    return run_cmd(["gh", *args], cwd=cwd, **kwargs)


def release_merge(
    *,
    base_branch: str = "release",
    head_branch: str = "main",
    version_files: list[str] | None = None,
) -> int:
    """Merge head_branch into base_branch with auto conflict resolution.

    Clones to a temp directory, merges, resolves version file conflicts,
    pushes, and creates a PR. Never touches the user's working tree.

    Args:
        base_branch: Target branch (default: release).
        head_branch: Source branch (default: main).
        version_files: Files to auto-resolve conflicts for.

    Returns:
        Exit code: 0=success, 1=error.
    """
    # Pre-flight: check gh CLI
    if not _gh_available():
        error("gh CLI not found")
        info("  Install: https://cli.github.com/")
        info("  brew install gh  OR  sudo apt install gh")
        _print_manual_commands(head_branch, base_branch)
        return 1

    if not _gh_authenticated():
        error("gh CLI not authenticated")
        info("  Run: gh auth login")
        _print_manual_commands(head_branch, base_branch)
        return 1

    # Get repo info from current directory (before we cd to temp)
    repo_url = _get_repo_url()
    repo_slug = _get_repo_slug()
    if not repo_url or not repo_slug:
        error("Could not detect repository from git remote")
        return 1

    files = version_files or VERSION_FILES
    merge_branch = f"chore/merge-to-{base_branch}"

    info(f"Release merge: {head_branch} -> {base_branch} ({repo_slug})")

    # Work in a temp directory — never touch the user's working tree
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-merge-") as tmp:
        tmp_path = Path(tmp)

        # Shallow clone with just the branches we need
        info("  Cloning repository...")
        _git(
            ["clone", repo_url, ".", "--no-checkout", "--filter=blob:none"],
            cwd=tmp_path,
            capture=True,
            check=True,
        )
        _git(
            ["fetch", "origin", base_branch, head_branch],
            cwd=tmp_path,
            capture=True,
            check=True,
        )

        # Check if there's anything to merge
        result = _git(
            ["log", "--oneline", f"origin/{base_branch}..origin/{head_branch}"],
            cwd=tmp_path,
            capture=True,
            check=True,
        )
        commits = result.stdout.strip()
        if not commits:
            warn(f"No new commits on {head_branch} — nothing to merge")
            return 0

        commit_count = len(commits.splitlines())
        info(f"  {commit_count} commit(s) to merge")

        # Configure git identity for the merge commit
        _git(
            ["config", "user.name", "github-actions[bot]"],
            cwd=tmp_path,
            check=True,
        )
        _git(
            ["config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            cwd=tmp_path,
            check=True,
        )

        # Create merge branch from base
        _git(
            ["checkout", f"origin/{base_branch}"],
            cwd=tmp_path,
            capture=True,
            check=True,
        )
        _git(
            ["checkout", "-b", merge_branch],
            cwd=tmp_path,
            capture=True,
            check=True,
        )

        # Attempt merge
        merge_result = _git(
            ["merge", f"origin/{head_branch}", "--no-edit"],
            cwd=tmp_path,
            capture=True,
            check=False,
        )

        if merge_result.returncode == 0:
            info("  Merge completed cleanly")
        else:
            info("  Merge has conflicts — resolving version files")
            resolved = 0

            for filepath in files:
                check = _git(
                    ["diff", "--name-only", "--diff-filter=U", "--", filepath],
                    cwd=tmp_path,
                    capture=True,
                    check=False,
                )
                if check.stdout.strip():
                    info(f"    Resolved: {filepath} (keeping {base_branch} version)")
                    _git(
                        ["checkout", "--ours", "--", filepath],
                        cwd=tmp_path,
                        check=True,
                    )
                    _git(["add", "--", filepath], cwd=tmp_path, check=True)
                    resolved += 1

            # Check for remaining unresolved conflicts
            remaining = _git(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=tmp_path,
                capture=True,
                check=False,
            )
            if remaining.stdout.strip():
                error(f"  Unresolved conflicts in: {remaining.stdout.strip()}")
                error("  These require manual resolution")
                _print_manual_commands(head_branch, base_branch)
                return 1

            if resolved > 0:
                info(f"  Resolved {resolved} version file conflict(s)")

            _git(["commit", "--no-edit"], cwd=tmp_path, capture=True, check=True)

        # Delete remote merge branch if it exists from a previous run
        _git(
            ["push", "origin", "--delete", merge_branch],
            cwd=tmp_path,
            capture=True,
            check=False,
        )

        # Push merge branch
        info("  Pushing merge branch...")
        try:
            _git(
                ["push", "-u", "origin", merge_branch],
                cwd=tmp_path,
                capture=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            error("Failed to push merge branch")
            return 1

        # Create PR via gh
        pr_body = (
            f"## Automated Release Merge\n\n"
            f"Merging `{head_branch}` into `{base_branch}` ({commit_count} commits).\n\n"
            f"### Commits\n\n```\n{commits}\n```"
        )

        info("  Creating PR...")
        try:
            pr_result = _gh(
                [
                    "pr",
                    "create",
                    "--repo",
                    repo_slug,
                    "--base",
                    base_branch,
                    "--head",
                    merge_branch,
                    "--title",
                    f"chore: merge {head_branch} into {base_branch}",
                    "--body",
                    pr_body,
                ],
                cwd=tmp_path,
                capture=True,
            )
            pr_url = pr_result.stdout.strip()
            success(f"Release merge PR: {pr_url}")
        except subprocess.CalledProcessError:
            error("Failed to create PR")
            info(f"  Branch '{merge_branch}' was pushed — create the PR manually:")
            info(
                f"  gh pr create --base {base_branch} --head {merge_branch} "
                f'--title "chore: merge {head_branch} into {base_branch}"'
            )
            return 1

    return 0
