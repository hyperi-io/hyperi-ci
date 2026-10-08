# Project:   HyperI CI
# File:      tests/unit/test_gh.py
# Purpose:   Tests for shared GitHub CLI helpers -- gh detection, and the
#            pinned run selection watch/logs rely on (issue #101)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci import gh
from hyperi_ci.gh import (
    RunSelectionError,
    describe_run,
    gh_api,
    list_runs,
    project_ci_workflow,
    require_gh,
    select_run,
)

_SHA = "a" * 40
_OTHER_SHA = "b" * 40


def _run(
    run_id: int,
    workflow: str,
    *,
    sha: str = _SHA,
    event: str = "push",
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict:
    """One entry as `gh run list --json` returns it."""
    return {
        "databaseId": run_id,
        "workflowName": workflow,
        "headSha": sha,
        "headBranch": "main",
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "url": f"https://github.com/hyperi-io/hyperi-ci/actions/runs/{run_id}",
    }


class _Gh:
    """Stands in for the gh process: records argv and the --input file it saw."""

    def __init__(self, returncode: int = 0, stdout: str = "{}", stderr: str = ""):
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.cmd: list[str] = []
        self.input_path: Path | None = None
        self.input_body: object = None

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.cmd = cmd
        if "--input" in cmd:
            self.input_path = Path(cmd[cmd.index("--input") + 1])
            self.input_body = json.loads(self.input_path.read_text(encoding="utf-8"))
        return self.result


class TestGhApi:
    def test_a_body_goes_through_a_temp_file_that_is_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _Gh(stdout='{"sha": "abc"}')
        monkeypatch.setattr(gh, "run_cmd", fake)
        body = {"tree": [{"path": "VERSION", "sha": None}]}
        outcome = gh_api(["-X", "POST", "repos/o/r/git/trees"], body=body)
        assert outcome.data == {"sha": "abc"}
        assert fake.cmd[:5] == ["gh", "api", "-X", "POST", "repos/o/r/git/trees"]
        assert fake.input_body == body
        assert fake.input_path is not None and not fake.input_path.exists()

    def test_no_body_sends_no_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _Gh()
        monkeypatch.setattr(gh, "run_cmd", fake)
        gh_api(["repos/o/r"])
        assert fake.cmd == ["gh", "api", "repos/o/r"]

    def test_the_temp_file_is_removed_when_gh_cannot_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Path] = []

        def boom(cmd: list[str], **kwargs: object) -> None:
            seen.append(Path(cmd[cmd.index("--input") + 1]))
            raise OSError("gh: not found")

        monkeypatch.setattr(gh, "run_cmd", boom)
        with pytest.raises(OSError):
            gh_api(["repos/o/r"], body={"a": 1})
        assert seen and not seen[0].exists()

    def test_a_failure_carries_gh_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gh, "run_cmd", _Gh(1, "", "HTTP 422: refused\n"))
        outcome = gh_api(["-X", "PATCH", "repos/o/r/git/refs/heads/main"])
        assert outcome.data is None
        assert outcome.stderr == "HTTP 422: refused"
        assert outcome.reason == (
            "gh api repos/o/r/git/refs/heads/main failed: HTTP 422: refused"
        )

    def test_a_silent_failure_names_the_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gh, "run_cmd", _Gh(4, "", ""))
        outcome = gh_api(["repos/o/r"])
        assert (outcome.data, outcome.stderr) == (None, "")
        assert outcome.reason.endswith("failed: exit 4")

    def test_output_that_is_not_json_is_no_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gh, "run_cmd", _Gh(0, "<html>oops</html>"))
        outcome = gh_api(["repos/o/r"])
        assert (outcome.data, outcome.stderr) == (None, None)
        assert "returned no JSON" in outcome.reason


class TestRequireGh:
    """Tests for gh CLI detection."""

    def test_returns_true_when_gh_found(self) -> None:
        with patch("hyperi_ci.gh.shutil.which", return_value="/usr/bin/gh"):
            assert require_gh() is True

    def test_returns_false_when_gh_missing(self) -> None:
        with patch("hyperi_ci.gh.shutil.which", return_value=None):
            assert require_gh() is False


class TestSelectRunPin:
    """The sha pin: only runs built from the commit asked about qualify."""

    def test_picks_the_single_run_for_the_sha(self) -> None:
        runs = [_run(2, "CI", sha=_OTHER_SHA), _run(1, "CI")]
        assert select_run(runs, head_sha=_SHA)["databaseId"] == 1

    def test_sha_match_is_case_insensitive(self) -> None:
        runs = [_run(1, "CI", sha=_SHA.upper())]
        assert select_run(runs, head_sha=_SHA)["databaseId"] == 1

    def test_no_run_for_the_sha_raises(self) -> None:
        runs = [_run(2, "CI", sha=_OTHER_SHA)]
        with pytest.raises(
            RunSelectionError, match="No runs found for commit aaaaaaaa"
        ):
            select_run(runs, head_sha=_SHA)

    def test_empty_list_raises(self) -> None:
        with pytest.raises(RunSelectionError, match="No runs found"):
            select_run([], head_sha=_SHA)


class TestSelectRunAmbiguity:
    """Several runs on one commit is a refusal, never a guess (#101)."""

    def test_refuses_and_names_every_candidate(self) -> None:
        runs = [
            _run(1, "Dependency Graph"),
            _run(2, "Test", status="in_progress", conclusion=None),
        ]
        with pytest.raises(RunSelectionError) as exc:
            select_run(runs, head_sha=_SHA)
        message = str(exc.value)
        assert "2 runs match" in message
        assert "Dependency Graph" in message
        assert "Test" in message
        # The ids are the point: a refusal has to be actionable.
        assert "actions/runs/1" in message
        assert "actions/runs/2" in message
        assert "--workflow" in message

    def test_refuses_when_one_workflow_has_two_runs_on_the_commit(self) -> None:
        # push and pull_request both fire CI for the same commit; naming
        # the workflow does not separate them, so refuse with the events
        # visible rather than picking the newer.
        runs = [
            _run(1, "CI", event="push"),
            _run(2, "CI", event="pull_request"),
        ]
        with pytest.raises(RunSelectionError) as exc:
            select_run(runs, head_sha=_SHA, workflow="CI")
        message = str(exc.value)
        assert "push" in message
        assert "pull_request" in message
        assert "run ids" in message

    def test_the_issue_101_case_resolves_once_the_workflow_is_named(self) -> None:
        # Reported green off Dependency Graph while Test was still going.
        runs = [
            _run(1, "Dependency Graph"),
            _run(2, "Test", status="in_progress", conclusion=None),
        ]
        chosen = select_run(runs, head_sha=_SHA, workflow="Test")
        assert chosen["databaseId"] == 2
        assert chosen["status"] == "in_progress"


class TestSelectRunWorkflowMatch:
    """Workflow narrowing: case-insensitive, exact before substring."""

    def test_matches_case_insensitively(self) -> None:
        runs = [_run(1, "Dependency Graph"), _run(2, "Test")]
        assert select_run(runs, head_sha=_SHA, workflow="test")["databaseId"] == 2

    def test_exact_match_beats_substring(self) -> None:
        runs = [_run(1, "CI"), _run(2, "CI Nightly")]
        assert select_run(runs, head_sha=_SHA, workflow="CI")["databaseId"] == 1

    def test_substring_matches_when_no_exact(self) -> None:
        runs = [_run(1, "Dependency Graph"), _run(2, "Rust CI")]
        assert select_run(runs, head_sha=_SHA, workflow="rust")["databaseId"] == 2

    def test_unknown_workflow_lists_what_is_there(self) -> None:
        runs = [_run(1, "Dependency Graph"), _run(2, "Test")]
        with pytest.raises(RunSelectionError) as exc:
            select_run(runs, head_sha=_SHA, workflow="ci.yml")
        message = str(exc.value)
        assert "No run matches workflow 'ci.yml'" in message
        assert "Dependency Graph, Test" in message

    def test_workflow_filter_applies_after_the_sha_pin(self) -> None:
        # A Test run on ANOTHER commit must not satisfy --workflow Test.
        runs = [_run(1, "Test", sha=_OTHER_SHA), _run(2, "Docs")]
        with pytest.raises(RunSelectionError, match="No run matches workflow 'Test'"):
            select_run(runs, head_sha=_SHA, workflow="Test")


class TestDescribeRun:
    """Refusal messages have to identify the run they are talking about."""

    def test_carries_id_workflow_event_and_state(self) -> None:
        line = describe_run(_run(7, "Test", status="in_progress", conclusion=None))
        assert "7" in line
        assert "Test" in line
        assert "[push]" in line
        assert "in_progress/pending" in line

    def test_tolerates_missing_fields(self) -> None:
        assert describe_run({}) == "?  ?  [?]  ?/pending"


class TestListRuns:
    """`list_runs` shapes the gh call; it never filters by workflow there."""

    def test_passes_commit_and_limit(self) -> None:
        with patch("hyperi_ci.gh.gh_json", return_value=[]) as mock_json:
            list_runs(commit=_SHA, limit=7)
        args = mock_json.call_args[0][0]
        assert args[args.index("--commit") + 1] == _SHA
        assert args[args.index("--limit") + 1] == "7"

    def test_never_delegates_the_workflow_filter_to_gh(self) -> None:
        # gh's --workflow takes a name OR a filename; select_run matches
        # on the name alone, so two matchers would drop runs silently.
        with patch("hyperi_ci.gh.gh_json", return_value=[]) as mock_json:
            list_runs(branch="main")
        assert "--workflow" not in mock_json.call_args[0][0]

    def test_repo_is_forwarded(self) -> None:
        with patch("hyperi_ci.gh.gh_json", return_value=[]) as mock_json:
            list_runs(repo="hyperi-io/dfe-loader")
        args = mock_json.call_args[0][0]
        assert args[args.index("--repo") + 1] == "hyperi-io/dfe-loader"


class TestProjectCiWorkflow:
    """The default pin comes from the project's declared CI workflow."""

    def _write(self, root: Path, body: str) -> Path:
        workflows = root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(body, encoding="utf-8")
        return root

    def test_reads_the_declared_name(self, tmp_path: Path) -> None:
        self._write(tmp_path, "name: CI\non:\n  push:\n")
        assert project_ci_workflow(cwd=tmp_path) == "CI"

    def test_none_without_a_ci_workflow(self, tmp_path: Path) -> None:
        assert project_ci_workflow(cwd=tmp_path) is None

    def test_none_when_the_workflow_names_nothing(self, tmp_path: Path) -> None:
        # GitHub falls back to the file path as the display name, which
        # is not what `gh run list` reports as workflowName.
        self._write(tmp_path, "on:\n  push:\n")
        assert project_ci_workflow(cwd=tmp_path) is None

    def test_none_on_unparseable_yaml(self, tmp_path: Path) -> None:
        self._write(tmp_path, "name: [unclosed\n")
        assert project_ci_workflow(cwd=tmp_path) is None
