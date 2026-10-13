# Project:   HyperI CI
# File:      tests/unit/test_versions_audit_pr.py
# Purpose:   Tests for scripts/versions-audit-pr.py
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The rolling pin-bump PR: what it bumps, what it says, and what it does."""

import base64
import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "versions_audit_pr", _ROOT / "scripts" / "versions-audit-pr.py"
)
assert _SPEC is not None and _SPEC.loader is not None
vap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vap)

_OLD = {
    "tools": {
        "gitleaks": {"version": "v8.1.0", "repo": "gitleaks/gitleaks"},
        "golangci-lint": {
            "version": "v2.14.0",
            "pin": ".github/actions/setup-go-tools/action.yml",
        },
        "vulture": {"version": "2.16", "pin": ".github/workflows/ci.yml"},
        "semgrep": {"version": "1.179.0"},
    },
    "runtimes": {"node": 24},
}


def _bumped(**versions: str) -> dict:
    new = yaml.safe_load(yaml.safe_dump(_OLD))
    for name, version in versions.items():
        new["tools"][name]["version"] = version
    return new


class Recorder:
    """Stands in for run_cmd: records argv, answers git show and gh pr list."""

    def __init__(self, versions_yaml: str = "", prs: str = "") -> None:
        self.calls: list[list[str]] = []
        self.envs: list[object] = []
        self._versions_yaml = versions_yaml
        self._prs = prs

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        self.envs.append(kwargs.get("env"))
        out = ""
        if cmd[:2] == ["git", "show"]:
            out = self._versions_yaml
        elif cmd[:3] == ["gh", "pr", "list"]:
            out = self._prs
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def verbs(self) -> list[str]:
        return [" ".join(c[:3]) for c in self.calls]


class TestBumpsBetween:
    def test_no_change_is_no_bump(self) -> None:
        assert vap.bumps_between(_OLD, _bumped()) == []

    def test_a_version_change_is_a_bump(self) -> None:
        bumps = vap.bumps_between(_OLD, _bumped(gitleaks="v8.2.0"))
        assert bumps == [vap.Bump("gitleaks", "v8.1.0", "v8.2.0", ())]

    def test_a_composite_pin_is_marked(self) -> None:
        (bump,) = vap.bumps_between(_OLD, _bumped(**{"golangci-lint": "v2.15.0"}))
        assert bump.composites == (".github/actions/setup-go-tools/action.yml",)

    def test_a_workflow_pin_is_not_a_composite(self) -> None:
        (bump,) = vap.bumps_between(_OLD, _bumped(vulture="2.17"))
        assert bump.composites == ()

    def test_a_pin_list_keeps_only_the_composites(self) -> None:
        new = _bumped(semgrep="1.180.0")
        new["tools"]["semgrep"]["pin"] = [
            ".github/workflows/ci.yml",
            ".github/actions/setup-x/action.yml",
        ]
        (bump,) = vap.bumps_between(_OLD, new)
        assert bump.composites == (".github/actions/setup-x/action.yml",)

    def test_a_new_tool_is_not_a_bump(self) -> None:
        new = _bumped()
        new["tools"]["newcomer"] = {"version": "1.0.0"}
        assert vap.bumps_between(_OLD, new) == []


class TestRenderBody:
    def test_lists_every_bump(self) -> None:
        body = vap.render_body(
            [vap.Bump("gitleaks", "v8.1.0", "v8.2.0", ()), vap.Bump("ty", "1", "2", ())]
        )
        assert "- gitleaks v8.1.0 -> v8.2.0" in body
        assert "- ty 1 -> 2" in body

    def test_no_composite_no_gate(self) -> None:
        body = vap.render_body([vap.Bump("gitleaks", "v8.1.0", "v8.2.0", ())])
        assert vap.COMPOSITE_GATE not in body

    def test_a_composite_bump_carries_the_gate_and_the_file(self) -> None:
        path = ".github/actions/setup-go-tools/action.yml"
        body = vap.render_body([vap.Bump("golangci-lint", "a", "b", (path,))])
        assert "composite default changes: owner approval needed before merge" in body
        assert f"- golangci-lint -> {path}" in body


class TestDecide:
    @pytest.mark.parametrize(
        ("clean", "bumps", "prs", "want"),
        [
            (True, [], [], "noop"),
            (True, [], [7], "close"),
            (False, [], [], "noop"),
            (False, [], [7], "noop"),
            (False, ["b"], [], "open"),
            (False, ["b"], [7], "update"),
        ],
    )
    def test_table(self, clean: bool, bumps: list, prs: list, want: str) -> None:
        assert vap.decide(clean, bumps, prs) == want


class TestAuthEnv:
    def test_token_goes_in_an_extraheader(self) -> None:
        env = vap.auth_env("s3cret", {})
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        encoded = env["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic ")
        assert base64.b64decode(encoded).decode() == "x-access-token:s3cret"

    def test_appends_after_existing_entries(self) -> None:
        env = vap.auth_env("t", {"GIT_CONFIG_COUNT": "2"})
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert "GIT_CONFIG_KEY_2" in env


def _tree(tmp_path: Path, new: dict) -> Path:
    target = tmp_path / vap.VERSIONS_REL
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(new), encoding="utf-8")
    return tmp_path


class TestSync:
    def test_drift_with_no_pr_opens_one(self, tmp_path: Path) -> None:
        run = Recorder(yaml.safe_dump(_OLD))
        cwd = _tree(tmp_path, _bumped(gitleaks="v8.2.0"))
        action = vap.sync(drift_clean=False, repo="o/r", token="t", cwd=cwd, run=run)
        assert action == "open"
        i = next(i for i, c in enumerate(run.calls) if c[:2] == ["git", "push"])
        # The only ref this run may force-update is the bot branch.
        assert run.calls[i][-1] == f"HEAD:refs/heads/{vap.BRANCH}"
        env = run.envs[i]
        assert isinstance(env, dict)
        assert "extraheader" in env["GIT_CONFIG_KEY_0"]
        commit = next(c for c in run.calls if "commit" in c)
        assert vap.COMMIT_SUBJECT in commit
        assert "gh pr create" in run.verbs()

    def test_drift_with_an_open_pr_updates_it(self, tmp_path: Path) -> None:
        run = Recorder(yaml.safe_dump(_OLD), prs="12\n")
        cwd = _tree(tmp_path, _bumped(gitleaks="v8.2.0"))
        action = vap.sync(drift_clean=False, repo="o/r", token="t", cwd=cwd, run=run)
        assert action == "update"
        assert "gh pr create" not in run.verbs()
        assert ["gh", "pr", "edit", "12"] == run.calls[-1][:4]

    def test_a_second_open_pr_is_closed(self, tmp_path: Path) -> None:
        run = Recorder(yaml.safe_dump(_OLD), prs="15\n12\n")
        cwd = _tree(tmp_path, _bumped(gitleaks="v8.2.0"))
        vap.sync(drift_clean=False, repo="o/r", token="t", cwd=cwd, run=run)
        assert run.calls[-2][:4] == ["gh", "pr", "edit", "12"]
        assert run.calls[-1][:4] == ["gh", "pr", "close", "15"]

    def test_no_drift_closes_the_open_pr(self, tmp_path: Path) -> None:
        run = Recorder(yaml.safe_dump(_OLD), prs="12\n")
        cwd = _tree(tmp_path, _bumped())
        action = vap.sync(drift_clean=True, repo="o/r", token="t", cwd=cwd, run=run)
        assert action == "close"
        assert run.calls[-1][:4] == ["gh", "pr", "close", "12"]
        assert vap.CLOSE_COMMENT in run.calls[-1]
        assert not any(c[:2] == ["git", "push"] for c in run.calls)

    def test_drift_nothing_could_write_touches_nothing(self, tmp_path: Path) -> None:
        run = Recorder(yaml.safe_dump(_OLD), prs="12\n")
        cwd = _tree(tmp_path, _bumped())
        action = vap.sync(drift_clean=False, repo="o/r", token="t", cwd=cwd, run=run)
        assert action == "noop"
        assert run.verbs() == ["git show HEAD:" + vap.VERSIONS_REL, "gh pr list"]


class TestCommitAgainstRealGit:
    def test_commits_the_bumped_tree_on_the_bot_branch(self, tmp_path: Path) -> None:
        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(tmp_path), *args],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout

        git("init", "-q", "-b", "main")
        target = _tree(tmp_path, _OLD) / vap.VERSIONS_REL
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        target.write_text(yaml.safe_dump(_bumped(gitleaks="v8.2.0")), "utf-8")
        (tmp_path / "untracked.txt").write_text("x", "utf-8")

        def no_push(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            if cmd[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return vap.run_cmd(cmd, **kwargs)  # type: ignore[arg-type]

        bumps = [vap.Bump("gitleaks", "v8.1.0", "v8.2.0", ())]
        vap.commit_and_push(bumps, "t", tmp_path, no_push)

        assert git("branch", "--show-current").strip() == vap.BRANCH
        assert git("log", "-1", "--format=%s").strip() == vap.COMMIT_SUBJECT
        assert "- gitleaks v8.1.0 -> v8.2.0" in git("log", "-1", "--format=%b")
        assert git("show", "--name-only", "--format=").split() == [vap.VERSIONS_REL]
