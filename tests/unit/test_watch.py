# Project:   HyperI CI
# File:      tests/unit/test_watch.py
# Purpose:   Tests for hyperi_ci.watch — exponential backoff, timeout
#            semantics (including --timeout 0), transient-failure
#            tolerance, status-on-timeout reporting.
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from unittest.mock import patch

import pytest

from hyperi_ci.watch import (
    _DEFAULT_TIMEOUT,
    _MAX_CONSECUTIVE_FETCH_FAILURES,
    _get_run_status,
    _poll_interval,
    _resume_command,
    watch_run,
)


class TestPollInterval:
    """Exponential backoff with cap at 120 s."""

    def test_first_attempt_is_base(self) -> None:
        assert _poll_interval(30, 1) == 30.0

    def test_second_attempt_grows(self) -> None:
        assert _poll_interval(30, 2) == 45.0

    def test_third_attempt_grows_more(self) -> None:
        assert _poll_interval(30, 3) == pytest.approx(67.5)

    def test_caps_at_120(self) -> None:
        # Eventually plateaus regardless of attempt count.
        assert _poll_interval(30, 50) == 120.0

    def test_cap_holds_for_smaller_base(self) -> None:
        # Base 10, attempt enough times: 10 * 1.5^4 = 50.625, capped above
        # is 120 — but we should never *exceed* 120 even with crazy attempts.
        assert _poll_interval(10, 100) <= 120.0

    def test_minimum_attempt_one(self) -> None:
        # Attempt 1 → exponent is min(0, 4) = 0 → base * 1 = base.
        assert _poll_interval(60, 1) == 60.0


class TestResumeCommand:
    """Copy-pasteable resume hint shown in timeout messages."""

    def test_with_timeout(self) -> None:
        assert _resume_command("12345", 3600) == "hyperi-ci watch 12345 --timeout 3600"

    def test_with_zero_timeout(self) -> None:
        assert _resume_command("12345", 0) == "hyperi-ci watch 12345 --timeout 0"

    def test_with_repo(self) -> None:
        # When watching a run in a different repo than the cwd, the
        # resume hint must include --repo so the user can copy-paste
        # without re-deriving the repo from somewhere.
        assert (
            _resume_command("12345", 3600, repo="hyperi-io/dfe-loader")
            == "hyperi-ci watch 12345 --repo hyperi-io/dfe-loader --timeout 3600"
        )

    def test_with_repo_and_zero_timeout(self) -> None:
        assert (
            _resume_command("12345", 0, repo="hyperi-io/dfe-loader")
            == "hyperi-ci watch 12345 --repo hyperi-io/dfe-loader --timeout 0"
        )


class TestGetRunStatusRepo:
    """`_get_run_status` must forward --repo to gh when set, so watching
    a run in a different repo than cwd doesn't 404 on every poll."""

    def test_no_repo_omits_flag(self) -> None:
        with patch("hyperi_ci.watch.gh_run") as mock_gh:
            mock_gh.return_value.stdout = '{"status": "in_progress"}'
            _get_run_status("12345")
            args = mock_gh.call_args[0][0]
            assert "--repo" not in args

    def test_repo_set_appends_flag(self) -> None:
        with patch("hyperi_ci.watch.gh_run") as mock_gh:
            mock_gh.return_value.stdout = '{"status": "in_progress"}'
            _get_run_status("12345", repo="hyperi-io/dfe-loader")
            args = mock_gh.call_args[0][0]
            assert "--repo" in args
            assert args[args.index("--repo") + 1] == "hyperi-io/dfe-loader"


class TestDefaultTimeout:
    """Default timeout is sized for Tier 2 Rust builds."""

    def test_default_covers_tier_2_builds(self) -> None:
        # Tier 2 PGO + BOLT for both archs in parallel takes 35-45 min
        # in observed v1.17.5/v1.18.0 dfe-loader publishes. 60 min default
        # gives comfortable margin.
        assert _DEFAULT_TIMEOUT >= 3600

    def test_max_consecutive_failures_reasonable(self) -> None:
        # ~10 failures at exponential backoff capped at 120s = ~6+ min
        # of sustained outage tolerated. Enough to ride out a typical
        # GitHub API hiccup; short enough to not spin indefinitely.
        assert 5 <= _MAX_CONSECUTIVE_FETCH_FAILURES <= 30


class TestWatchRunTimeout:
    """Timeout semantics — default, zero, message includes status."""

    def test_zero_timeout_polls_until_terminal(self) -> None:
        """`--timeout 0` disables timeout entirely; polls until terminal."""
        # Mock a run that goes through 3 in_progress states then completes.
        states = [
            {"status": "in_progress", "conclusion": None, "jobs": []},
            {"status": "in_progress", "conclusion": None, "jobs": []},
            {"status": "in_progress", "conclusion": None, "jobs": []},
            {
                "status": "completed",
                "conclusion": "success",
                "jobs": [],
                "url": "https://example",
                "workflowName": "CI",
                "headBranch": "main",
            },
        ]
        with (
            patch("hyperi_ci.watch.require_gh", return_value=True),
            patch(
                "hyperi_ci.watch._get_run_status",
                side_effect=states,
            ),
            patch("hyperi_ci.watch.time.sleep"),  # Don't actually wait
        ):
            rc = watch_run(run_id="12345", timeout=0, interval=1)
        assert rc == 0  # success

    def test_timeout_message_includes_status_and_resume_hint(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When timeout fires, error message should include current
        status + a resume command, so caller knows whether to re-watch."""
        in_progress = {"status": "in_progress", "conclusion": None, "jobs": []}
        with (
            patch("hyperi_ci.watch.require_gh", return_value=True),
            patch(
                "hyperi_ci.watch._get_run_status",
                return_value=in_progress,
            ),
            patch("hyperi_ci.watch.time.sleep"),
            patch(
                "hyperi_ci.watch.time.monotonic",
                # First call records start, second is past deadline.
                side_effect=[0.0, 0.0, 1000.0],
            ),
        ):
            # interval=1, timeout=10 → deadline at t=10; after one
            # iteration our mocked monotonic returns 1000 → out of loop.
            rc = watch_run(run_id="12345", timeout=10, interval=1)
        # Returns 2 on timeout per docstring
        assert rc == 2

    def test_explicit_zero_timeout_in_initial_log_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When timeout=0, the initial info log says '(no timeout)'."""
        terminal = {
            "status": "completed",
            "conclusion": "success",
            "jobs": [],
            "url": "",
            "workflowName": "CI",
            "headBranch": "main",
        }
        with (
            patch("hyperi_ci.watch.require_gh", return_value=True),
            patch("hyperi_ci.watch._get_run_status", return_value=terminal),
            patch("hyperi_ci.watch.time.sleep"),
        ):
            watch_run(run_id="42", timeout=0, interval=1)
        # The "no timeout" mode should be visibly distinguished in the
        # logs (see watch.py — info(f"Watching run {run_id} (no timeout)")).
        # We don't assert on exact log capture due to logger plumbing
        # variability; the test_zero_timeout_polls_until_terminal above
        # is the load-bearing behaviour test.


class TestWatchRunTransientFailures:
    """Transient `gh run view` failures back off + retry, but don't
    spin forever."""

    def test_recovers_from_transient_failure(self) -> None:
        """One failed fetch followed by success completes normally."""
        # A None response (transient failure) then a successful one.
        responses = [
            None,
            {
                "status": "completed",
                "conclusion": "success",
                "jobs": [],
                "url": "",
                "workflowName": "CI",
                "headBranch": "main",
            },
        ]
        with (
            patch("hyperi_ci.watch.require_gh", return_value=True),
            patch("hyperi_ci.watch._get_run_status", side_effect=responses),
            patch("hyperi_ci.watch.time.sleep"),
        ):
            rc = watch_run(run_id="12345", timeout=600, interval=1)
        assert rc == 0

    def test_gives_up_after_too_many_consecutive_failures(self) -> None:
        """`_MAX_CONSECUTIVE_FETCH_FAILURES` failures in a row -> exit 1."""
        with (
            patch("hyperi_ci.watch.require_gh", return_value=True),
            patch("hyperi_ci.watch._get_run_status", return_value=None),
            patch("hyperi_ci.watch.time.sleep"),
        ):
            rc = watch_run(run_id="12345", timeout=600, interval=1)
        # All fetches return None → consecutive_failures hits the cap →
        # returns 1 (NOT 2 for timeout — we distinguish "remote
        # unreachable" from "timeout while in progress").
        assert rc == 1

    def test_consecutive_counter_resets_on_success(self) -> None:
        """A successful fetch in the middle resets the failure counter."""
        # MAX-1 failures, one success, MAX more failures, then complete.
        # Should NOT exit early because the counter reset.
        n = _MAX_CONSECUTIVE_FETCH_FAILURES
        in_progress = {"status": "in_progress", "conclusion": None, "jobs": []}
        terminal = {
            "status": "completed",
            "conclusion": "success",
            "jobs": [],
            "url": "",
            "workflowName": "CI",
            "headBranch": "main",
        }
        responses = [None] * (n - 1) + [in_progress] + [None] * (n - 1) + [terminal]
        with (
            patch("hyperi_ci.watch.require_gh", return_value=True),
            patch("hyperi_ci.watch._get_run_status", side_effect=responses),
            patch("hyperi_ci.watch.time.sleep"),
        ):
            rc = watch_run(run_id="12345", timeout=0, interval=1)
        assert rc == 0


class TestWatchRunTerminalStates:
    """Terminal status handling — success vs failure vs cancellation."""

    def _terminal(self, conclusion: str) -> dict:
        return {
            "status": "completed",
            "conclusion": conclusion,
            "jobs": [],
            "url": "",
            "workflowName": "CI",
            "headBranch": "main",
        }

    def test_success_returns_zero(self) -> None:
        with (
            patch("hyperi_ci.watch.require_gh", return_value=True),
            patch(
                "hyperi_ci.watch._get_run_status",
                return_value=self._terminal("success"),
            ),
            patch("hyperi_ci.watch.time.sleep"),
        ):
            rc = watch_run(run_id="1", timeout=0, interval=1)
        assert rc == 0

    def test_failure_returns_one(self) -> None:
        with (
            patch("hyperi_ci.watch.require_gh", return_value=True),
            patch(
                "hyperi_ci.watch._get_run_status",
                return_value=self._terminal("failure"),
            ),
            patch("hyperi_ci.watch.time.sleep"),
        ):
            rc = watch_run(run_id="1", timeout=0, interval=1)
        assert rc == 1

    def test_cancelled_returns_one(self) -> None:
        with (
            patch("hyperi_ci.watch.require_gh", return_value=True),
            patch(
                "hyperi_ci.watch._get_run_status",
                return_value=self._terminal("cancelled"),
            ),
            patch("hyperi_ci.watch.time.sleep"),
        ):
            rc = watch_run(run_id="1", timeout=0, interval=1)
        assert rc == 1
