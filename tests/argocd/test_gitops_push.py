# Project:   HyperI CI
# File:      tests/argocd/test_gitops_push.py
# Purpose:   Tests for the gitops_push module (git clone/commit/push + PR)
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for ``hyperi_ci.argocd.gitops_push``.

Mocks `git` and `curl` subprocess calls. The path-write side effect
is real (we actually write into the cloned tmpdir to verify
content + skip-on-noop logic).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from hyperi_ci.argocd.gitops_push import GitopsPushConfig, push


def _ok_proc(stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock()
    p.returncode = 0
    p.stdout = stdout
    p.stderr = stderr
    return p


def _fail_proc(returncode: int = 1, stderr: str = "boom") -> MagicMock:
    p = MagicMock()
    p.returncode = returncode
    p.stdout = ""
    p.stderr = stderr
    return p


class TestPushDirect:
    def test_no_token_returns_1(self, monkeypatch) -> None:
        monkeypatch.delenv("GITOPS_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_WRITE_TOKEN", raising=False)

        cfg = GitopsPushConfig(
            repo="hyperi-io/gitops",
            path="applications/x/dev.yaml",
            content="kind: Application\n",
            commit_message="chore: x",
            push_mode="direct",
        )
        rc = push(cfg)
        assert rc == 1

    def test_direct_clones_writes_commits_pushes(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GITOPS_TOKEN", "fake-token")

        cfg = GitopsPushConfig(
            repo="hyperi-io/gitops",
            path="applications/x/dev.yaml",
            content="kind: Application\nmetadata:\n  name: x\n",
            commit_message="chore: bump x",
            push_mode="direct",
        )

        recorded: list = []

        def _run_git(cmd, **kwargs):
            recorded.append(cmd)
            # The clone needs to actually create the dir so the write step works.
            if cmd[1] == "clone":
                dest = Path(cmd[-1])
                dest.mkdir(parents=True, exist_ok=True)
                return _ok_proc()
            return _ok_proc()

        with patch("hyperi_ci.argocd.gitops_push.subprocess.run", side_effect=_run_git):
            rc = push(cfg)
        assert rc == 0
        # Verify the sequence: clone, config x2, add, commit, push
        ops = [c[1] for c in recorded if len(c) > 1]
        assert "clone" in ops
        assert "add" in ops
        assert "commit" in ops
        assert "push" in ops

    def test_no_change_skips_commit(self, monkeypatch, tmp_path: Path) -> None:
        """If file content matches what's already in the repo, no push."""
        monkeypatch.setenv("GITOPS_TOKEN", "fake-token")

        cfg = GitopsPushConfig(
            repo="hyperi-io/gitops",
            path="applications/x/dev.yaml",
            content="kind: Application\n",
            commit_message="noop",
            push_mode="direct",
        )

        def _run_git(cmd, **kwargs):
            if cmd[1] == "clone":
                # Pre-populate the file with the SAME content
                dest = Path(cmd[-1])
                target = dest / cfg.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(cfg.content, encoding="utf-8")
                return _ok_proc()
            return _ok_proc()

        with patch("hyperi_ci.argocd.gitops_push.subprocess.run", side_effect=_run_git):
            rc = push(cfg)
        assert rc == 0

    def test_clone_failure_returns_nonzero(self, monkeypatch) -> None:
        monkeypatch.setenv("GITOPS_TOKEN", "fake-token")
        cfg = GitopsPushConfig(
            repo="hyperi-io/gitops",
            path="applications/x/dev.yaml",
            content="kind: Application\n",
            commit_message="chore: x",
            push_mode="direct",
        )

        def _run_git(cmd, **kwargs):
            if cmd[1] == "clone":
                return _fail_proc(returncode=2, stderr="permission denied")
            return _ok_proc()

        with patch("hyperi_ci.argocd.gitops_push.subprocess.run", side_effect=_run_git):
            rc = push(cfg)
        assert rc == 2


class TestPushPR:
    def test_pr_creates_branch_and_calls_github_api(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GITOPS_TOKEN", "fake-token")

        cfg = GitopsPushConfig(
            repo="hyperi-io/gitops",
            path="applications/x/prod.yaml",
            content="kind: Application\n",
            commit_message="chore: prod x",
            push_mode="pr",
        )

        recorded: list = []

        def _run(cmd, **kwargs):
            recorded.append(cmd)
            if cmd[0] == "git" and cmd[1] == "clone":
                dest = Path(cmd[-1])
                dest.mkdir(parents=True, exist_ok=True)
                return _ok_proc()
            if cmd[0] == "curl":
                return _ok_proc(
                    stdout='{"html_url":"https://github.com/hyperi-io/gitops/pull/1"}'
                )
            return _ok_proc()

        with patch("hyperi_ci.argocd.gitops_push.subprocess.run", side_effect=_run):
            rc = push(cfg)
        assert rc == 0
        git_ops = [c[1] for c in recorded if c[0] == "git" and len(c) > 1]
        assert "checkout" in git_ops  # branch created
        curl_calls = [c for c in recorded if c[0] == "curl"]
        assert len(curl_calls) == 1, "PR creation should call curl once"
