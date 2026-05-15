# Project:   HyperI CI
# File:      src/hyperi_ci/argocd/gitops_push.py
# Purpose:   Clone GitOps repo, write Application YAML, commit + push (or PR)
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Push an ArgoCD Application YAML into the central GitOps repo.

Modes per env config:

* ``direct`` (default): clone main, write file, commit, push to main.
  Used for dev / staging environments.
* ``pr``: clone main, write file on a branch, push branch, open PR.
  Used for prod environments where a human approver is required.

Concurrency: relies on the GitHub Actions concurrency group at the
workflow level to serialise gitops pushes per-app per-env. This module
itself is single-shot (one push per invocation).

Auth: requires ``GITOPS_TOKEN`` (preferred) or ``GITHUB_TOKEN``
with ``contents: write`` to the gitops repo.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import error, info, success


@dataclass(frozen=True, slots=True)
class GitopsPushConfig:
    """Per-call configuration for a gitops push."""

    repo: str  # "hyperi-io/gitops"
    path: str  # "applications/dfe-loader/dev.yaml"
    content: str  # the Application YAML
    commit_message: str
    push_mode: str  # "direct" | "pr"
    branch_main: str = "main"


def push(cfg: GitopsPushConfig) -> int:
    """Execute the push. Returns exit code."""
    token = (
        os.environ.get("GITOPS_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GITHUB_WRITE_TOKEN")
    )
    if not token:
        error(
            f"No GITOPS_TOKEN / GITHUB_TOKEN in environment — can't push to {cfg.repo}"
        )
        return 1

    info(f"  argocd: pushing {cfg.path} to {cfg.repo} (mode={cfg.push_mode})")

    with tempfile.TemporaryDirectory(prefix="hyperi-gitops-") as tmpdir:
        clone_dir = Path(tmpdir) / "gitops"
        rc = _git_clone(cfg.repo, clone_dir, token=token)
        if rc != 0:
            return rc

        target = clone_dir / cfg.path
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        if existing == cfg.content:
            info(f"  argocd: {cfg.path} already up-to-date — no push needed")
            return 0
        target.write_text(cfg.content, encoding="utf-8", newline="\n")

        if cfg.push_mode == "pr":
            return _push_pr(
                clone_dir=clone_dir,
                target=target,
                cfg=cfg,
                token=token,
            )
        return _push_direct(clone_dir=clone_dir, cfg=cfg, token=token)


# ---- internals ----------------------------------------------------------


def _git_clone(repo: str, dest: Path, *, token: str) -> int:
    """Clone via HTTPS + token. Shallow clone (depth=1) for speed."""
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    proc = _run_git(["clone", "--depth=1", "--no-tags", url, str(dest)], cwd=Path.cwd())
    if proc.returncode != 0:
        # Don't print the URL (contains the token).
        error(f"git clone {repo} failed (exit {proc.returncode})")
        if proc.stderr:
            # Strip the token from any URLs in stderr.
            sanitised = proc.stderr.replace(token, "***")
            error(sanitised.rstrip())
        return proc.returncode
    _git_setup_identity(dest)
    return 0


def _git_setup_identity(repo: Path) -> None:
    """Configure a default committer identity inside the cloned repo."""
    actor = os.environ.get("GITHUB_ACTOR", "hyperi-ci")
    email = f"{actor}@users.noreply.github.com"
    _run_git(["config", "user.name", actor], cwd=repo, check=True)
    _run_git(["config", "user.email", email], cwd=repo, check=True)


def _push_direct(
    *,
    clone_dir: Path,
    cfg: GitopsPushConfig,
    token: str,
) -> int:
    rel = Path(cfg.path)
    proc = _run_git(["add", str(rel)], cwd=clone_dir)
    if proc.returncode != 0:
        error(proc.stderr.rstrip() if proc.stderr else "git add failed")
        return proc.returncode
    proc = _run_git(["commit", "-m", cfg.commit_message], cwd=clone_dir)
    if proc.returncode != 0:
        # `nothing to commit` is a no-op success
        if "nothing to commit" in (proc.stdout or "") + (proc.stderr or ""):
            info("  argocd: nothing to commit — content unchanged")
            return 0
        error(proc.stderr.rstrip() if proc.stderr else "git commit failed")
        return proc.returncode

    proc = _run_git(["push", "origin", cfg.branch_main], cwd=clone_dir)
    if proc.returncode != 0:
        error(proc.stderr.replace(token, "***").rstrip())
        return proc.returncode

    success(f"  argocd: pushed {cfg.path} to {cfg.repo}@{cfg.branch_main}")
    return 0


def _push_pr(
    *,
    clone_dir: Path,
    target: Path,
    cfg: GitopsPushConfig,
    token: str,
) -> int:
    """Create a branch, push it, open a PR via GitHub API."""
    branch_name = (
        f"hyperi-ci/{cfg.path.replace('/', '-').replace('.yaml', '')}"
        f"-{int(time.time())}"
    )
    proc = _run_git(["checkout", "-b", branch_name], cwd=clone_dir)
    if proc.returncode != 0:
        error(proc.stderr.rstrip())
        return proc.returncode

    proc = _run_git(["add", cfg.path], cwd=clone_dir)
    if proc.returncode != 0:
        error(proc.stderr.rstrip())
        return proc.returncode
    proc = _run_git(["commit", "-m", cfg.commit_message], cwd=clone_dir)
    if proc.returncode != 0:
        if "nothing to commit" in (proc.stdout or "") + (proc.stderr or ""):
            info("  argocd: nothing to commit on PR branch — no PR opened")
            return 0
        error(proc.stderr.rstrip())
        return proc.returncode

    proc = _run_git(["push", "origin", branch_name], cwd=clone_dir)
    if proc.returncode != 0:
        error(proc.stderr.replace(token, "***").rstrip())
        return proc.returncode

    return _open_pr(cfg=cfg, branch=branch_name, token=token)


def _open_pr(*, cfg: GitopsPushConfig, branch: str, token: str) -> int:
    """Open a GitHub PR via the REST API."""
    title = cfg.commit_message.splitlines()[0]
    body = (
        f"Automated push from hyperi-ci.\n\n"
        f"Updates `{cfg.path}` in this gitops repo.\n\n"
        f"Merge to apply the change to the corresponding ArgoCD env.\n"
    )
    payload = {
        "title": title,
        "head": branch,
        "base": cfg.branch_main,
        "body": body,
    }
    cmd = [
        "curl",
        "-fsSL",
        "-X",
        "POST",
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        f"https://api.github.com/repos/{cfg.repo}/pulls",
        "-d",
        json.dumps(payload),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        error(proc.stderr.replace(token, "***").rstrip())
        return proc.returncode
    try:
        pr = json.loads(proc.stdout)
        url = pr.get("html_url")
        success(f"  argocd: opened PR {url}")
    except json.JSONDecodeError:
        success(f"  argocd: PR opened (response: {proc.stdout[:200]})")
    return 0


def _run_git(
    args: list[str], *, cwd: Path, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )
