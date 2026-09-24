# Project:   HyperI CI
# File:      src/hyperi_ci/argocd/gitops_push.py
# Purpose:   Clone GitOps repo, write Application YAML, commit + push (or PR)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
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
with ``contents: write`` to the gitops repo. The token never goes on a child's
argv, where any process on the host can read it from ``/proc/<pid>/cmdline``,
and never into the clone's ``.git/config``. git gets it through ``GIT_CONFIG_*``
environment variables and curl through a config line on stdin.
"""

import base64
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success
from hyperi_ci.curl_config import config_line


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
            f"No GITOPS_TOKEN / GITHUB_TOKEN in environment -- can't push to {cfg.repo}"
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
            info(f"  argocd: {cfg.path} already up-to-date -- no push needed")
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


def _basic_credential(token: str) -> str:
    """Encode the token as the HTTP basic credential GitHub accepts for git."""
    return base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")


def _git_auth_env(token: str) -> dict[str, str]:
    """Build the environment that authenticates git to github.com with ``token``.

    git reads ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_<n>`` /
    ``GIT_CONFIG_VALUE_<n>`` as command-line config, which is how
    actions/checkout authenticates, so the header stays out of argv and out of
    the clone's ``.git/config``. The entry goes after any the environment
    already carries, so those still apply.

    Args:
        token: GitHub token with write access to the gitops repo.

    Returns:
        Variables for ``run_cmd(env=...)``, merged onto ``os.environ`` there.

    """
    try:
        index = max(int(os.environ.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        index = 0
    return {
        "GIT_CONFIG_COUNT": str(index + 1),
        f"GIT_CONFIG_KEY_{index}": "http.https://github.com/.extraheader",
        f"GIT_CONFIG_VALUE_{index}": f"AUTHORIZATION: basic {_basic_credential(token)}",
        # A rejected header must fail the call, not wait on a password prompt.
        "GIT_TERMINAL_PROMPT": "0",
    }


def _redact(text: str, token: str) -> str:
    """Mask the token, raw and in its basic-auth encoding, in child output."""
    return text.replace(token, "***").replace(_basic_credential(token), "***")


def _git_clone(repo: str, dest: Path, *, token: str) -> int:
    """Clone over HTTPS, shallow (depth=1) for speed."""
    url = f"https://github.com/{repo}.git"
    proc = _run_git(
        ["clone", "--depth=1", "--no-tags", url, str(dest)],
        cwd=Path.cwd(),
        env=_git_auth_env(token),
    )
    if proc.returncode != 0:
        error(f"git clone {repo} failed (exit {proc.returncode})")
        if proc.stderr:
            error(_redact(proc.stderr, token).rstrip())
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
            info("  argocd: nothing to commit -- content unchanged")
            return 0
        error(proc.stderr.rstrip() if proc.stderr else "git commit failed")
        return proc.returncode

    proc = _run_git(
        ["push", "origin", cfg.branch_main],
        cwd=clone_dir,
        env=_git_auth_env(token),
    )
    if proc.returncode != 0:
        error(_redact(proc.stderr, token).rstrip())
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
            info("  argocd: nothing to commit on PR branch -- no PR opened")
            return 0
        error(proc.stderr.rstrip())
        return proc.returncode

    proc = _run_git(
        ["push", "origin", branch_name],
        cwd=clone_dir,
        env=_git_auth_env(token),
    )
    if proc.returncode != 0:
        error(_redact(proc.stderr, token).rstrip())
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
    # No retry flags: a retried POST can open the PR twice.
    cmd = [
        "curl",
        "-fsSL",
        "-X",
        "POST",
        "-K",
        "-",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        f"https://api.github.com/repos/{cfg.repo}/pulls",
        "-d",
        json.dumps(payload),
    ]
    proc = run_cmd(
        cmd,
        capture=True,
        check=False,
        stdin_text=config_line("header", f"Authorization: Bearer {token}"),
    )
    if proc.returncode != 0:
        error(_redact(proc.stderr, token).rstrip())
        return proc.returncode
    try:
        pr = json.loads(proc.stdout)
        url = pr.get("html_url")
        success(f"  argocd: opened PR {url}")
    except json.JSONDecodeError:
        success(f"  argocd: PR opened (response: {proc.stdout[:200]})")
    return 0


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` with its output captured.

    Args:
        args: Arguments after ``git``.
        cwd: Directory to run in.
        check: Raise ``CalledProcessError`` on a non-zero exit.
        env: Extra environment, such as :func:`_git_auth_env` for a call that
            talks to the remote.

    Returns:
        The finished process.

    """
    return run_cmd(["git", *args], cwd=cwd, capture=True, check=check, env=env)
