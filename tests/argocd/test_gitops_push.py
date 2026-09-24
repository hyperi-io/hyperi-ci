# Project:   HyperI CI
# File:      tests/argocd/test_gitops_push.py
# Purpose:   Tests for the gitops_push module (git clone/commit/push + PR)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for ``hyperi_ci.argocd.gitops_push``.

Mocks the `git` and `curl` calls at ``run_cmd``. The path-write side effect
is real (we actually write into the cloned tmpdir to verify
content + skip-on-noop logic).
"""

import base64
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hyperi_ci.argocd import gitops_push
from hyperi_ci.argocd.gitops_push import GitopsPushConfig, push
from hyperi_ci.common import run_cmd

_TOKEN = "tok_TESTONLY_abc123"
_BASIC = base64.b64encode(f"x-access-token:{_TOKEN}".encode()).decode("ascii")


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


def _cfg(push_mode: str) -> GitopsPushConfig:
    return GitopsPushConfig(
        repo="hyperi-io/gitops",
        path=f"applications/x/{'prod' if push_mode == 'pr' else 'dev'}.yaml",
        content="kind: Application\nmetadata:\n  name: x\n",
        commit_message="chore: bump x",
        push_mode=push_mode,
    )


def _recording_runner(
    calls: list[tuple[list[str], dict]],
) -> Callable[..., MagicMock]:
    """Stand in for run_cmd: record every call, create the clone dir, answer curl."""

    def fake(cmd: list[str], **kwargs: object) -> MagicMock:
        calls.append((cmd, kwargs))
        if cmd[:2] == ["git", "clone"]:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        if cmd[0] == "curl":
            return _ok_proc(
                stdout='{"html_url":"https://github.com/hyperi-io/gitops/pull/1"}'
            )
        return _ok_proc()

    return fake


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

        with patch("hyperi_ci.argocd.gitops_push.run_cmd", side_effect=_run_git):
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

        with patch("hyperi_ci.argocd.gitops_push.run_cmd", side_effect=_run_git):
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

        with patch("hyperi_ci.argocd.gitops_push.run_cmd", side_effect=_run_git):
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

        with patch("hyperi_ci.argocd.gitops_push.run_cmd", side_effect=_run):
            rc = push(cfg)
        assert rc == 0
        git_ops = [c[1] for c in recorded if c[0] == "git" and len(c) > 1]
        assert "checkout" in git_ops  # branch created
        curl_calls = [c for c in recorded if c[0] == "curl"]
        assert len(curl_calls) == 1, "PR creation should call curl once"


class TestTheTokenStaysOffArgv:
    """argv is readable by any process on the host through /proc/<pid>/cmdline."""

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict]]:
        monkeypatch.delenv("GITOPS_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        recorded: list[tuple[list[str], dict]] = []
        monkeypatch.setattr(gitops_push, "run_cmd", _recording_runner(recorded))
        return recorded

    @pytest.mark.parametrize("push_mode", ["direct", "pr"])
    def test_no_argv_carries_the_token(
        self, calls: list[tuple[list[str], dict]], push_mode: str
    ) -> None:
        assert push(_cfg(push_mode)) == 0
        leaked = [
            cmd for cmd, _ in calls if any(_TOKEN in a or _BASIC in a for a in cmd)
        ]
        assert not leaked

    def test_the_clone_url_carries_no_credential(
        self, calls: list[tuple[list[str], dict]]
    ) -> None:
        assert push(_cfg("direct")) == 0
        [clone] = [cmd for cmd, _ in calls if cmd[:2] == ["git", "clone"]]
        assert "https://github.com/hyperi-io/gitops.git" in clone

    @pytest.mark.parametrize("push_mode", ["direct", "pr"])
    def test_every_remote_call_gets_the_header_through_env(
        self, calls: list[tuple[list[str], dict]], push_mode: str
    ) -> None:
        assert push(_cfg(push_mode)) == 0
        remote = [
            kwargs.get("env") or {}
            for cmd, kwargs in calls
            if cmd[0] == "git" and cmd[1] in {"clone", "push", "fetch"}
        ]
        assert len(remote) == 2
        for env in remote:
            assert env["GIT_CONFIG_COUNT"] == "1"
            assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
            assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {_BASIC}"

    def test_curl_reads_the_bearer_header_from_stdin(
        self, calls: list[tuple[list[str], dict]]
    ) -> None:
        assert push(_cfg("pr")) == 0
        [(cmd, kwargs)] = [(c, k) for c, k in calls if c[0] == "curl"]
        assert cmd[cmd.index("-K") + 1] == "-"
        assert kwargs["stdin_text"] == f'header = "Authorization: Bearer {_TOKEN}"\n'
        assert not any("Authorization" in arg for arg in cmd)

    def test_the_pr_post_is_not_retried(
        self, calls: list[tuple[list[str], dict]]
    ) -> None:
        assert push(_cfg("pr")) == 0
        [cmd] = [c for c, _ in calls if c[0] == "curl"]
        assert not [arg for arg in cmd if arg.startswith("--retry")]

    @pytest.mark.parametrize("failing", ["clone", "push"])
    def test_failure_output_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch, failing: str
    ) -> None:
        monkeypatch.setenv("GITOPS_TOKEN", _TOKEN)
        logged: list[str] = []
        monkeypatch.setattr(gitops_push, "error", logged.append)

        def fake(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[:2] == ["git", "clone"]:
                Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            if cmd[:2] == ["git", failing]:
                return _fail_proc(stderr=f"fatal: {_TOKEN} rejected ({_BASIC})")
            return _ok_proc()

        monkeypatch.setattr(gitops_push, "run_cmd", fake)
        assert push(_cfg("direct")) == 1
        text = "\n".join(logged)
        assert "***" in text
        assert _TOKEN not in text
        assert _BASIC not in text


class TestGitAuthEnv:
    def test_an_existing_env_config_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        env = gitops_push._git_auth_env(_TOKEN)
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_2"] == "http.https://github.com/.extraheader"
        assert "GIT_CONFIG_KEY_0" not in env

    @pytest.mark.parametrize("bad", ["", "x", "-4"])
    def test_an_unusable_count_starts_at_zero(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", bad)
        env = gitops_push._git_auth_env(_TOKEN)
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert "GIT_CONFIG_KEY_0" in env

    def test_a_rejected_token_never_prompts(self) -> None:
        assert gitops_push._git_auth_env(_TOKEN)["GIT_TERMINAL_PROMPT"] == "0"

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
    def test_real_git_resolves_the_header_for_the_gitops_remote(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "kept")
        env = {
            **gitops_push._git_auth_env(_TOKEN),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        def git_config(*args: str) -> str:
            result = run_cmd(
                ["git", "config", *args], capture=True, cwd=tmp_path, env=env
            )
            return result.stdout.strip()

        header = git_config(
            "--get-urlmatch",
            "http.extraheader",
            "https://github.com/hyperi-io/gitops.git",
        )
        assert header == f"AUTHORIZATION: basic {_BASIC}"
        assert git_config("--get", "user.name") == "kept"
