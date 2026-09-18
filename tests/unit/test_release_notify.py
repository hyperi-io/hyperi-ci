# Project:   HyperI CI
# File:      tests/unit/test_release_notify.py
# Purpose:   A release announces itself, once, and never breaks the release
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Notifications must be idempotent and incapable of failing a release.

Re-running a publish is normal here, so a second run must not double-comment.
And a notification that returns non-zero would turn an already-shipped release
red, which is worse than the missing notification it was meant to fix.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.release_notify import (
    _previous_tag,
    failure_issue_numbers,
    notify_failure,
    notify_slack,
    notify_success,
    referenced_issues,
)


def _git(stdout: str, returncode: int = 0) -> MagicMock:
    return MagicMock(stdout=stdout, returncode=returncode)


class TestPreviousTag:
    """The previous tag bounds the commit range the release notes are built from."""

    def test_a_prerelease_does_not_bound_the_range(self) -> None:
        tags = "v1.1.2-beta.1\nv1.1.1\n"
        with patch("hyperi_ci.release_notify.run_cmd", return_value=_git(tags)):
            assert _previous_tag("1.1.2") == "v1.1.1"

    def test_a_prerelease_only_repo_has_no_previous_tag(self) -> None:
        tags = "v1.1.2-beta.1\n"
        with patch("hyperi_ci.release_notify.run_cmd", return_value=_git(tags)):
            assert _previous_tag("1.1.2") is None

    def test_a_prerelease_being_notified_finds_its_own_predecessor(self) -> None:
        """The tag being released stays a candidate whatever its shape."""
        tags = "v1.2.0\nv1.1.2-beta.1\nv1.1.1\n"
        with patch("hyperi_ci.release_notify.run_cmd", return_value=_git(tags)):
            assert _previous_tag("1.1.2-beta.1") == "v1.1.1"


class TestReferencedIssues:
    def test_finds_a_squash_merge_reference(self) -> None:
        log = "fix(deps): tighten the floor (#412)\n\nRefs #77\n"
        with patch("hyperi_ci.release_notify.run_cmd", return_value=_git(log)):
            assert referenced_issues("1.2.3") == [77, 412]

    def test_deduplicates(self) -> None:
        log = "fix: a (#5)\n\ncloses #5\n"
        with patch("hyperi_ci.release_notify.run_cmd", return_value=_git(log)):
            assert referenced_issues("1.2.3") == [5]

    def test_ignores_a_colour_literal(self) -> None:
        """`#fff` is not issue 0, and `#1a2` is not issue 1."""
        log = "style: set the banner to #fff and the border to #1a2\n"
        with patch("hyperi_ci.release_notify.run_cmd", return_value=_git(log)):
            assert referenced_issues("1.2.3") == []

    def test_empty_when_git_fails(self) -> None:
        with patch("hyperi_ci.release_notify.run_cmd", return_value=_git("", 128)):
            assert referenced_issues("1.2.3") == []


class TestNotifySuccess:
    @pytest.fixture(autouse=True)
    def repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/hyperi-ci")

    def test_comments_once_per_issue(self) -> None:
        with patch("hyperi_ci.release_notify.referenced_issues", return_value=[7, 9]):
            with patch(
                "hyperi_ci.release_notify._already_commented", return_value=False
            ):
                with patch(
                    "hyperi_ci.release_notify._api", return_value={"id": 1}
                ) as api:
                    assert notify_success(version="1.2.3") == 0
        posts = [c for c in api.call_args_list if "POST" in c.args[0]]
        assert len(posts) == 2

    def test_skips_an_issue_already_announced(self) -> None:
        """A re-run must not double-comment."""
        with patch("hyperi_ci.release_notify.referenced_issues", return_value=[7]):
            with patch(
                "hyperi_ci.release_notify._already_commented", return_value=True
            ):
                with patch("hyperi_ci.release_notify._api", return_value=[]) as api:
                    assert notify_success(version="1.2.3") == 0
        posts = [c for c in api.call_args_list if "POST" in c.args[0]]
        assert posts == []

    def test_no_references_is_not_a_failure(self) -> None:
        with patch("hyperi_ci.release_notify.referenced_issues", return_value=[]):
            assert notify_success(version="1.2.3") == 0

    def test_a_failed_comment_still_returns_zero(self) -> None:
        """A #123 may point at another repo, or an issue since deleted."""
        with patch("hyperi_ci.release_notify.referenced_issues", return_value=[7]):
            with patch(
                "hyperi_ci.release_notify._already_commented", return_value=False
            ):
                with patch("hyperi_ci.release_notify._api", return_value=None):
                    assert notify_success(version="1.2.3") == 0

    def test_no_repository_is_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        assert notify_success(version="1.2.3") == 0


class TestFailureIssueNumbers:
    """A retry that ships closes exactly its own version's failure issue."""

    def test_matches_the_version_exactly(self) -> None:
        issues = [
            {"number": 160, "title": "Release v2.10.3 failed"},
            {"number": 161, "title": "Release v2.10.30 failed"},
            {"number": 162, "title": "Release v2.10.2 failed"},
        ]
        assert failure_issue_numbers(issues, "2.10.3") == [160]

    def test_ignores_an_unrelated_issue_with_the_label(self) -> None:
        issues = [{"number": 7, "title": "flaky release tail"}]
        assert failure_issue_numbers(issues, "2.10.3") == []

    def test_an_api_error_matches_nothing(self) -> None:
        """`_api` returns None on failure; that must not close anything."""
        assert failure_issue_numbers(None, "2.10.3") == []
        assert failure_issue_numbers({"message": "Not Found"}, "2.10.3") == []

    def test_skips_malformed_entries(self) -> None:
        issues = ["not-a-dict", {"title": "Release v2.10.3 failed"}]
        assert failure_issue_numbers(issues, "2.10.3") == []


class TestNotifyFailure:
    @pytest.fixture(autouse=True)
    def repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/hyperi-ci")

    def test_opens_an_issue(self) -> None:
        with patch(
            "hyperi_ci.release_notify._api", side_effect=[[], {"number": 42}]
        ) as api:
            assert notify_failure(version="1.2.3", run_url="http://run") == 0
        created = api.call_args_list[-1]
        assert created.kwargs["body"]["title"] == "Release v1.2.3 failed"

    def test_reuses_an_open_issue_for_the_same_version(self) -> None:
        """A retried release must not open a second issue."""
        existing = [{"number": 42, "title": "Release v1.2.3 failed"}]
        with patch("hyperi_ci.release_notify._api", return_value=existing) as api:
            assert notify_failure(version="1.2.3") == 0
        assert api.call_count == 1

    def test_the_issue_names_the_retry_commands(self) -> None:
        with patch(
            "hyperi_ci.release_notify._api", side_effect=[[], {"number": 42}]
        ) as api:
            notify_failure(version="1.2.3", run_url="http://run")
        body = api.call_args_list[-1].kwargs["body"]["body"]
        assert "hyperi-ci publish --version 1.2.3" in body

    def test_an_api_failure_still_returns_zero(self) -> None:
        """The release already failed; do not fail it twice."""
        with patch("hyperi_ci.release_notify._api", return_value=None):
            assert notify_failure(version="1.2.3") == 0


class TestSlackIsOffByDefault:
    def test_no_webhook_configured_posts_nothing(self) -> None:
        with patch("hyperi_ci.release_notify.run_cmd") as spawned:
            assert notify_slack(CIConfig(_raw={}), text="hi") == 0
        spawned.assert_not_called()

    def test_a_named_variable_that_is_unset_posts_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_CI_WEBHOOK", raising=False)
        config = CIConfig(
            _raw={"notify": {"slack": {"webhook_env": "SLACK_CI_WEBHOOK"}}}
        )
        with patch("hyperi_ci.release_notify.run_cmd") as spawned:
            assert notify_slack(config, text="hi") == 0
        spawned.assert_not_called()

    def test_posts_when_configured_and_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLACK_CI_WEBHOOK", "https://hooks.example/x")
        config = CIConfig(
            _raw={"notify": {"slack": {"webhook_env": "SLACK_CI_WEBHOOK"}}}
        )
        with patch(
            "hyperi_ci.release_notify.run_cmd", return_value=MagicMock(returncode=0)
        ) as spawned:
            assert notify_slack(config, text="hi") == 0
        assert "https://hooks.example/x" in spawned.call_args.args[0]

    def test_the_webhook_url_is_never_in_config(self) -> None:
        """Config is committed; a webhook URL is a secret."""
        config = CIConfig(
            _raw={"notify": {"slack": {"webhook_env": "SLACK_CI_WEBHOOK"}}}
        )
        assert "https://" not in str(config.get("notify.slack.webhook_env"))
