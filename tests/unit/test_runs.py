# Project:   HyperI CI
# File:      tests/unit/test_runs.py
# Purpose:   Run anchors beyond HEAD, and the stand-down that replaces
#            "No runs found"
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for run resolution (issue #97).

The payloads are the shapes GitHub returned for hyperi-io/dfe-hyperdx:
one commit carrying four `pull_request` runs beside three `push` runs,
and the scheduled `upstream-sync` run on main that a feature-branch
checkout could not reach.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci import runs, workflows
from hyperi_ci.gh import RunSelectionError

_PR_SHA = "4ef1424d3784220259603d5867c4a6344470dd0b"
_MAIN_SHA = "e15a0503c3ffda9d9417f76af2125ed48cd386a7"

_SCAFFOLDED = """\
name: CI
'on':
  push:
jobs:
  ci:
    uses: hyperi-io/hyperi-ci/.github/workflows/ts-ci.yml@main
"""

_BESPOKE = "name: upstream-sync\n'on':\n  schedule:\n    - cron: '0 3 * * 1'\n"


def _run(
    run_id: int,
    workflow: str,
    *,
    sha: str = _PR_SHA,
    event: str = "pull_request",
    branch: str = "fix/upstream-sync-drift-alarm",
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict:
    """One entry as `gh run list --json` returns it."""
    return {
        "databaseId": run_id,
        "workflowName": workflow,
        "headSha": sha,
        "headBranch": branch,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "url": f"https://github.com/hyperi-io/dfe-hyperdx/actions/runs/{run_id}",
    }


# The seven runs GitHub held for one dfe-hyperdx commit.
_ONE_COMMIT = [
    _run(35556249712, "CI"),
    _run(35556249096, "fork-security"),
    _run(35556249038, "fork-surface"),
    _run(35556249022, "Docker Build"),
    _run(35556245272, "CI", event="push"),
    _run(35556244721, "fork-surface", event="push"),
    _run(35556244700, "fork-security", event="push"),
]

# The scheduled runs on main, which a feature-branch HEAD never matches.
_ON_MAIN = [
    _run(
        35570252326,
        "upstream-sync",
        sha=_MAIN_SHA,
        event="schedule",
        branch="main",
        conclusion="failure",
    ),
    _run(
        35565835116,
        "fork-security",
        sha=_MAIN_SHA,
        event="schedule",
        branch="main",
    ),
]


def _repo(root: Path, files: dict[str, str]) -> Path:
    """Write a .github/workflows tree and return the repo root."""
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True)
    for name, body in files.items():
        (directory / name).write_text(body, encoding="utf-8")
    return root


def workflows_owned(root: Path) -> list[str]:
    """Display names of the workflows hyperi-ci scaffolded in a repo."""
    return workflows.owned_names(workflows.inventory(root))


class TestResolveAnchor:
    """Which commit the caller pinned to."""

    def test_head_is_the_default(self) -> None:
        with patch("hyperi_ci.runs.get_head_sha", return_value=_PR_SHA):
            anchor = runs.resolve_anchor()
        assert anchor.sha == _PR_SHA
        assert anchor.local is True
        assert anchor.head is True
        assert "HEAD" in anchor.label

    def test_a_commit_is_taken_as_given(self) -> None:
        anchor = runs.resolve_anchor(commit=_MAIN_SHA)
        assert anchor.sha == _MAIN_SHA
        # This repo, so the project's own pin still applies...
        assert anchor.local is True
        # ...but it is not HEAD, so nothing is worth waiting for.
        assert anchor.head is False

    def test_another_repo_is_not_local(self) -> None:
        anchor = runs.resolve_anchor(commit=_MAIN_SHA, repo="hyperi-io/dfe-hyperdx")
        assert anchor.local is False

    def test_a_pr_resolves_to_its_head_commit(self) -> None:
        with patch(
            "hyperi_ci.runs.pr_head",
            return_value=(_PR_SHA, "fix/upstream-sync-drift-alarm"),
        ):
            anchor = runs.resolve_anchor(pr=18)
        assert anchor.sha == _PR_SHA
        assert anchor.branch == "fix/upstream-sync-drift-alarm"
        assert anchor.label == "PR #18"
        assert anchor.local is True

    def test_a_branch_anchors_on_its_newest_run(self) -> None:
        with patch("hyperi_ci.runs.list_runs", return_value=_ON_MAIN):
            anchor = runs.resolve_anchor(branch="main")
        assert anchor.sha == _MAIN_SHA
        assert anchor.branch == "main"

    def test_a_branch_with_no_runs_carries_no_sha(self) -> None:
        with patch("hyperi_ci.runs.list_runs", return_value=[]):
            anchor = runs.resolve_anchor(branch="fix/new")
        assert anchor.sha is None
        assert anchor.branch == "fix/new"

    def test_repo_alone_carries_no_sha(self) -> None:
        # A local HEAD says nothing about another repo's runs.
        anchor = runs.resolve_anchor(repo="hyperi-io/dfe-hyperdx")
        assert anchor.sha is None
        assert anchor.repo == "hyperi-io/dfe-hyperdx"

    def test_two_anchors_are_refused(self) -> None:
        with pytest.raises(RunSelectionError, match="both pin the lookup"):
            runs.resolve_anchor(branch="main", commit=_MAIN_SHA)

    def test_unreadable_head_names_the_alternatives(self) -> None:
        with (
            patch("hyperi_ci.runs.get_head_sha", return_value=None),
            pytest.raises(RunSelectionError, match="--branch / --commit / --pr"),
        ):
            runs.resolve_anchor()


class TestPrHead:
    """The argv and the parsing for a PR lookup."""

    def test_argv_asks_for_the_head_ref(self) -> None:
        payload = subprocess.CompletedProcess(
            [],
            0,
            f'{{"headRefOid":"{_PR_SHA}","headRefName":"fix/thing"}}',
            "",
        )
        with patch("hyperi_ci.runs.gh_run", return_value=payload) as mock_gh:
            sha, branch = runs.pr_head(18)
        args = mock_gh.call_args[0][0]
        assert args[:3] == ["pr", "view", "18"]
        assert "headRefOid" in args[args.index("--json") + 1]
        assert (sha, branch) == (_PR_SHA, "fix/thing")

    def test_repo_is_forwarded(self) -> None:
        payload = subprocess.CompletedProcess(
            [], 0, f'{{"headRefOid":"{_PR_SHA}","headRefName":"main"}}', ""
        )
        with patch("hyperi_ci.runs.gh_run", return_value=payload) as mock_gh:
            runs.pr_head(18, repo="hyperi-io/dfe-hyperdx")
        args = mock_gh.call_args[0][0]
        assert args[args.index("--repo") + 1] == "hyperi-io/dfe-hyperdx"

    def test_an_unreadable_pr_is_refused(self) -> None:
        with (
            patch(
                "hyperi_ci.runs.gh_run",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ),
            pytest.raises(RunSelectionError, match="Could not read PR #18"),
        ):
            runs.pr_head(18)


class TestAnchorRuns:
    """The gh call an anchor shapes."""

    def test_a_sha_anchor_filters_by_commit(self) -> None:
        anchor = runs.Anchor(_PR_SHA, None, "commit", None, local=True)
        with patch("hyperi_ci.runs.list_runs", return_value=[]) as mock_list:
            runs.anchor_runs(anchor)
        assert mock_list.call_args.kwargs["commit"] == _PR_SHA
        assert mock_list.call_args.kwargs["branch"] is None

    def test_a_shaless_anchor_falls_back_to_the_branch(self) -> None:
        anchor = runs.Anchor(None, "main", "branch main", None, local=False)
        with patch("hyperi_ci.runs.list_runs", return_value=[]) as mock_list:
            runs.anchor_runs(anchor)
        assert mock_list.call_args.kwargs["branch"] == "main"


class TestPickDefaultPin:
    """The default pin is the repo's own ci.yml, whoever wrote it."""

    def test_a_repo_pins_on_its_own_ci(self, tmp_path: Path) -> None:
        # One commit carries seven runs; only the declared CI is assumed.
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED, "upstream-sync.yml": _BESPOKE})
        anchor = runs.Anchor(_PR_SHA, None, "commit", None, local=True)
        with pytest.raises(RunSelectionError, match="refusing to guess"):
            # Two CI runs on the commit (push and pull_request) is still
            # ambiguous -- the pin narrows, it does not guess.
            runs.pick(anchor, _ONE_COMMIT, project_dir=tmp_path)

    def test_the_pin_resolves_when_one_ci_run_exists(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED, "upstream-sync.yml": _BESPOKE})
        anchor = runs.Anchor(_PR_SHA, None, "commit", None, local=True)
        candidates = [r for r in _ONE_COMMIT if r["event"] == "pull_request"]
        chosen = runs.pick(anchor, candidates, project_dir=tmp_path)
        assert chosen["databaseId"] == 35556249712

    def test_a_ci_yml_we_did_not_scaffold_still_pins(self, tmp_path: Path) -> None:
        # A fork keeping upstream's CI must not be left refusing among
        # CodeQL and the audits -- that is the failing closed to avoid.
        body = _SCAFFOLDED.replace(
            "uses: hyperi-io/hyperi-ci/.github/workflows/ts-ci.yml@main",
            "steps:\n      - run: make test",
        )
        _repo(tmp_path, {"ci.yml": body, "upstream-sync.yml": _BESPOKE})
        assert workflows_owned(tmp_path) == []
        anchor = runs.Anchor(_PR_SHA, None, "commit", None, local=True)
        candidates = [r for r in _ONE_COMMIT if r["event"] == "pull_request"]
        chosen = runs.pick(anchor, candidates, project_dir=tmp_path)
        assert chosen["databaseId"] == 35556249712

    def test_no_pin_without_a_ci_workflow(self, tmp_path: Path) -> None:
        # Nothing declares a CI workflow, so every candidate is named.
        _repo(tmp_path, {"upstream-sync.yml": _BESPOKE})
        anchor = runs.Anchor(_PR_SHA, None, "commit", None, local=True)
        candidates = [r for r in _ONE_COMMIT if r["event"] == "pull_request"]
        with pytest.raises(RunSelectionError) as exc:
            runs.pick(anchor, candidates, project_dir=tmp_path)
        assert "Docker Build" in str(exc.value)

    def test_no_pin_for_a_foreign_repo(self, tmp_path: Path) -> None:
        # Another repo's ci.yml is not this checkout's to assume.
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED})
        anchor = runs.Anchor(
            _PR_SHA, None, "commit", "hyperi-io/dfe-hyperdx", local=False
        )
        candidates = [r for r in _ONE_COMMIT if r["event"] == "pull_request"]
        with pytest.raises(RunSelectionError, match="refusing to guess"):
            runs.pick(anchor, candidates, project_dir=tmp_path)

    def test_an_explicit_workflow_reaches_a_foreign_one(self, tmp_path: Path) -> None:
        # The light-touch case: naming a workflow hyperi-ci does not own.
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED, "upstream-sync.yml": _BESPOKE})
        anchor = runs.Anchor(_MAIN_SHA, "main", "branch main", None, local=False)
        chosen = runs.pick(
            anchor, _ON_MAIN, workflow="upstream-sync", project_dir=tmp_path
        )
        assert chosen["databaseId"] == 35570252326


class TestStandDown:
    """A refusal has to be actionable, not just negative."""

    def test_it_lists_the_runs_that_do_exist(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED})
        anchor = runs.Anchor(_PR_SHA, "main", "commit 4ef1424d", None, local=True)
        with patch("hyperi_ci.runs.list_runs", return_value=_ON_MAIN):
            message = runs.stand_down(
                anchor, reason="No runs found.", command="watch", project_dir=tmp_path
            )
        assert "35570252326" in message
        assert "upstream-sync" in message

    def test_it_marks_the_workflows_we_did_not_scaffold(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED})
        anchor = runs.Anchor(None, "main", "branch main", None, local=False)
        with patch("hyperi_ci.runs.list_runs", return_value=_ON_MAIN):
            message = runs.stand_down(
                anchor, reason="No runs found.", command="logs", project_dir=tmp_path
            )
        assert "did not scaffold" in message
        assert "upstream-sync" in message
        assert "fork-security" in message

    def test_it_prints_a_command_that_reaches_a_run(self, tmp_path: Path) -> None:
        anchor = runs.Anchor(None, None, "repo", "hyperi-io/dfe-hyperdx", local=False)
        with patch("hyperi_ci.runs.list_runs", return_value=_ON_MAIN):
            message = runs.stand_down(
                anchor, reason="Nothing pins it.", command="watch", project_dir=tmp_path
            )
        assert "hyperi-ci watch 35570252326 --repo hyperi-io/dfe-hyperdx" in message

    def test_an_empty_repo_says_so_plainly(self, tmp_path: Path) -> None:
        anchor = runs.Anchor(None, None, "repo", None, local=False)
        with patch("hyperi_ci.runs.list_runs", return_value=[]):
            message = runs.stand_down(
                anchor, reason="No runs found.", command="watch", project_dir=tmp_path
            )
        assert "no recent runs" in message

    def test_a_listing_failure_does_not_mask_the_refusal(self, tmp_path: Path) -> None:
        anchor = runs.Anchor(None, None, "repo", None, local=False)
        with patch(
            "hyperi_ci.runs.list_runs",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            message = runs.stand_down(
                anchor, reason="No runs found.", command="watch", project_dir=tmp_path
            )
        assert message.startswith("No runs found.")


class TestResolve:
    """The whole path, as watch/logs/rerun call it."""

    def test_a_pr_reaches_a_run_that_is_not_on_head(self, tmp_path: Path) -> None:
        # The issue #97 case: the run is on the PR, not the branch head.
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED})
        candidates = [r for r in _ONE_COMMIT if r["event"] == "pull_request"]
        with (
            patch("hyperi_ci.runs.pr_head", return_value=(_PR_SHA, "fix/x")),
            patch("hyperi_ci.runs.list_runs", return_value=candidates),
            patch("hyperi_ci.runs.get_head_sha") as mock_head,
        ):
            chosen = runs.resolve(pr=18, project_dir=tmp_path)
        assert chosen["databaseId"] == 35556249712
        mock_head.assert_not_called()

    def test_an_empty_result_stands_down_with_a_listing(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED})
        with (
            patch("hyperi_ci.runs.get_head_sha", return_value=_PR_SHA),
            patch("hyperi_ci.runs.list_runs", side_effect=[[], _ON_MAIN]),
            pytest.raises(RunSelectionError) as exc,
        ):
            runs.resolve(project_dir=tmp_path)
        message = str(exc.value)
        assert "No runs found for commit 4ef1424d" in message
        # The point of the change: what IS there, not just what is not.
        assert "upstream-sync" in message
        assert "hyperi-ci watch 35570252326" in message

    def test_repo_without_an_anchor_stands_down(self, tmp_path: Path) -> None:
        with (
            patch("hyperi_ci.runs.list_runs", return_value=_ON_MAIN),
            pytest.raises(RunSelectionError, match="Nothing pins the lookup"),
        ):
            runs.resolve(repo="hyperi-io/dfe-hyperdx", project_dir=tmp_path)
