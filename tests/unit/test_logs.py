# Project:   HyperI CI
# File:      tests/unit/test_logs.py
# Purpose:   Tests for hyperi_ci.logs — the run-log download lands the zip on
#            disk and extracts it, a failed download says why, and the run a
#            --failed report read is always named (issue #101).
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import io
import json
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci.logs import (
    _download_logs,
    _failed_job_names,
    _get_run,
    fetch_logs,
    logs_api_path,
)

_SHA = "d" * 40


@pytest.fixture(autouse=True)
def _no_declared_workflow():
    """Pin these cases explicitly, not off whatever ci.yml the cwd holds."""
    with patch("hyperi_ci.gh.project_ci_workflow", return_value=None):
        yield


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ci / Quality/1_Run quality checks.txt", "line one\nline two\n")
    return buf.getvalue()


def _listed_run(
    run_id: int,
    workflow: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    sha: str = _SHA,
) -> dict:
    """One entry as `gh run list --json` returns it."""
    return {
        "databaseId": run_id,
        "workflowName": workflow,
        "headSha": sha,
        "headBranch": "main",
        "event": "push",
        "status": status,
        "conclusion": conclusion,
        "url": f"https://github.com/hyperi-io/hyperi-ci/actions/runs/{run_id}",
    }


def _viewed_run(
    workflow: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    jobs: list[dict] | None = None,
) -> dict:
    """One run as `gh run view --json` returns it."""
    return {
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion,
        "event": "push",
        "headBranch": "main",
        "headSha": _SHA,
        "url": "https://github.com/hyperi-io/hyperi-ci/actions/runs/12",
        "jobs": jobs or [],
    }


class TestDownloadLogs:
    def test_zip_from_gh_api_is_written_and_extracted(self) -> None:
        def fake_run(cmd, **kwargs):
            assert cmd[:2] == ["gh", "api"]
            assert cmd[2].endswith("/actions/runs/424242/logs")
            kwargs["stdout"].write(_zip_bytes())
            return subprocess.CompletedProcess(cmd, 0)

        with patch("hyperi_ci.logs.subprocess.run", side_effect=fake_run):
            log_dir = _download_logs("424242")

        assert log_dir is not None
        extracted = sorted(
            p.relative_to(log_dir).as_posix() for p in log_dir.rglob("*.txt")
        )
        assert extracted == ["ci / Quality/1_Run quality checks.txt"]
        assert not (log_dir / "logs.zip").exists()

    def test_gh_api_failure_returns_none_with_the_reason(self) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr=b"HTTP 404: Not Found")

        with (
            patch("hyperi_ci.logs.subprocess.run", side_effect=fake_run),
            patch("hyperi_ci.logs.error") as err,
        ):
            assert _download_logs("424242") is None
        assert "HTTP 404" in err.call_args.args[0]

    def test_non_zip_response_returns_none(self) -> None:
        def fake_run(cmd, **kwargs):
            kwargs["stdout"].write(b"<html>not a zip</html>")
            return subprocess.CompletedProcess(cmd, 0)

        with (
            patch("hyperi_ci.logs.subprocess.run", side_effect=fake_run),
            patch("hyperi_ci.logs.error") as err,
        ):
            assert _download_logs("424242") is None
        assert "not a zip" in err.call_args.args[0]


class TestFailedJobNames:
    """Only a `failure` conclusion counts as a failed job."""

    def test_picks_failures_only(self) -> None:
        run = _viewed_run(
            "CI",
            jobs=[
                {"name": "Quality", "conclusion": "success"},
                {"name": "Test", "conclusion": "failure"},
                {"name": "Build", "conclusion": "skipped"},
            ],
        )
        assert _failed_job_names(run) == {"test"}

    def test_empty_when_nothing_failed(self) -> None:
        assert _failed_job_names(_viewed_run("CI")) == set()


class TestGetRun:
    """A run that cannot be read is not a run with no failures."""

    def test_returns_none_on_gh_error(self) -> None:
        with patch(
            "hyperi_ci.logs.gh_run",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            assert _get_run("12") is None

    def test_returns_none_on_bad_json(self) -> None:
        with patch("hyperi_ci.logs.gh_run") as mock_gh:
            mock_gh.return_value.stdout = "not json"
            assert _get_run("12") is None

    def test_parses_the_run(self) -> None:
        with patch("hyperi_ci.logs.gh_run") as mock_gh:
            mock_gh.return_value.stdout = json.dumps(_viewed_run("CI"))
            run = _get_run("12")
        assert run is not None
        assert run["workflowName"] == "CI"


class TestFetchLogsFailedOnly:
    """`--failed` says which run it read, so an empty result cannot pass
    for a green build (issue #101)."""

    def test_names_the_run_when_nothing_failed(self) -> None:
        with (
            patch("hyperi_ci.logs.require_gh", return_value=True),
            patch(
                "hyperi_ci.logs._get_run", return_value=_viewed_run("Dependency Graph")
            ),
            patch("hyperi_ci.logs._download_logs") as mock_download,
            patch("hyperi_ci.logs.warn") as mock_warn,
        ):
            rc = fetch_logs(run_id="12", failed_only=True)
        assert rc == 0
        messages = " ".join(str(call.args[0]) for call in mock_warn.call_args_list)
        assert "12" in messages
        assert "Dependency Graph" in messages
        assert "different run" in messages
        # Nothing to fetch, and nothing fetched.
        mock_download.assert_not_called()

    def test_says_so_when_the_run_is_still_going(self) -> None:
        still_going = _viewed_run("CI", status="in_progress", conclusion=None)
        with (
            patch("hyperi_ci.logs.require_gh", return_value=True),
            patch("hyperi_ci.logs._get_run", return_value=still_going),
            patch("hyperi_ci.logs.warn") as mock_warn,
        ):
            rc = fetch_logs(run_id="12", failed_only=True)
        assert rc == 0
        messages = " ".join(str(call.args[0]) for call in mock_warn.call_args_list)
        assert "has not finished" in messages

    def test_unreadable_run_is_an_error_not_a_clean_bill(self) -> None:
        with (
            patch("hyperi_ci.logs.require_gh", return_value=True),
            patch("hyperi_ci.logs._get_run", return_value=None),
            patch("hyperi_ci.logs._download_logs") as mock_download,
        ):
            rc = fetch_logs(run_id="12", failed_only=True)
        assert rc == 1
        mock_download.assert_not_called()

    def test_failed_job_is_fetched(self, tmp_path: Path) -> None:
        failing = _viewed_run(
            "CI",
            conclusion="failure",
            jobs=[{"name": "Test", "conclusion": "failure"}],
        )
        with (
            patch("hyperi_ci.logs.require_gh", return_value=True),
            patch("hyperi_ci.logs._get_run", return_value=failing),
            patch("hyperi_ci.logs._download_logs", return_value=tmp_path),
            patch("hyperi_ci.logs._filter_and_print") as mock_print,
        ):
            rc = fetch_logs(run_id="12", failed_only=True)
        assert rc == 0
        assert mock_print.call_args.kwargs["failed_jobs"] == {"test"}


class TestLogsApiPath:
    """The logs endpoint has no --repo flag, so the slug goes in the path."""

    def test_defaults_to_ghs_own_placeholders(self) -> None:
        assert logs_api_path("123") == "repos/{owner}/{repo}/actions/runs/123/logs"

    def test_a_named_repo_is_substituted(self) -> None:
        assert (
            logs_api_path("123", "hyperi-io/dfe-hyperdx")
            == "repos/hyperi-io/dfe-hyperdx/actions/runs/123/logs"
        )

    def test_the_download_uses_it(self) -> None:
        sent: list[str] = []

        def fake_run(cmd, **kwargs):
            sent.extend(cmd)
            kwargs["stdout"].write(_zip_bytes())
            return subprocess.CompletedProcess(cmd, 0)

        with patch("hyperi_ci.logs.subprocess.run", side_effect=fake_run):
            _download_logs("456", "hyperi-io/dfe-hyperdx")
        assert "repos/hyperi-io/dfe-hyperdx/actions/runs/456/logs" in sent


class TestFetchLogsPinning:
    """With no run id, logs pins on the commit at HEAD."""

    def test_a_pr_reaches_a_run_that_is_not_on_head(self, tmp_path: Path) -> None:
        failing = _viewed_run(
            "CI",
            conclusion="failure",
            jobs=[{"name": "Test", "conclusion": "failure"}],
        )
        with (
            patch("hyperi_ci.logs.require_gh", return_value=True),
            patch("hyperi_ci.runs.pr_head", return_value=("f" * 40, "fix/x")),
            patch(
                "hyperi_ci.runs.list_runs",
                return_value=[_listed_run(44, "CI", sha="f" * 40)],
            ),
            patch("hyperi_ci.logs._get_run", return_value=failing) as mock_get,
            patch("hyperi_ci.logs._download_logs", return_value=tmp_path),
            patch("hyperi_ci.logs._filter_and_print"),
        ):
            rc = fetch_logs(pr=18, failed_only=True)
        assert rc == 0
        assert mock_get.call_args[0][0] == "44"

    def test_refuses_an_ambiguous_commit(self) -> None:
        candidates = [
            _listed_run(11, "Dependency Graph"),
            _listed_run(12, "Test", conclusion="failure"),
        ]
        with (
            patch("hyperi_ci.logs.require_gh", return_value=True),
            patch("hyperi_ci.runs.get_head_sha", return_value=_SHA),
            patch("hyperi_ci.runs.list_runs", return_value=candidates),
            patch("hyperi_ci.runs.project_ci_workflow", return_value=None),
            patch("hyperi_ci.workflows.inventory", return_value=[]),
            patch("hyperi_ci.logs._download_logs") as mock_download,
        ):
            rc = fetch_logs(failed_only=True)
        assert rc == 1
        mock_download.assert_not_called()

    def test_workflow_pins_the_run_that_is_read(self, tmp_path: Path) -> None:
        candidates = [
            _listed_run(11, "Dependency Graph"),
            _listed_run(12, "Test", conclusion="failure"),
        ]
        failing = _viewed_run(
            "Test",
            conclusion="failure",
            jobs=[{"name": "Test", "conclusion": "failure"}],
        )
        with (
            patch("hyperi_ci.logs.require_gh", return_value=True),
            patch("hyperi_ci.runs.get_head_sha", return_value=_SHA),
            patch("hyperi_ci.runs.list_runs", return_value=candidates),
            patch("hyperi_ci.runs.project_ci_workflow", return_value=None),
            patch("hyperi_ci.workflows.inventory", return_value=[]),
            patch("hyperi_ci.logs._get_run", return_value=failing) as mock_get,
            patch("hyperi_ci.logs._download_logs", return_value=tmp_path),
            patch("hyperi_ci.logs._filter_and_print"),
        ):
            rc = fetch_logs(workflow="Test", failed_only=True)
        assert rc == 0
        assert mock_get.call_args[0][0] == "12"
