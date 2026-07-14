# Project:   HyperI CI
# File:      tests/unit/test_publish_mode.py
# Purpose:   Tests for the publish-mode SSOT (branch-mode tri-state)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Push-mode resolution matrix.

The env is injected as a plain dict — no os.environ monkeypatching, so
the matrix is exact regardless of what CI env the test itself runs in.
"""

from __future__ import annotations

from hyperi_ci.publish_mode import (
    DEV,
    PUBLISH,
    VALIDATE,
    dev_branch_slug,
    is_branch_ci_context,
    is_publish_mode,
    resolve_push_mode,
)

_PR_ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_EVENT_NAME": "pull_request",
    "GITHUB_REF": "refs/pull/7/merge",
    "GITHUB_HEAD_REF": "feat/dev-images",
}
_BRANCH_PUSH_ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_REF": "refs/heads/feat/dev-images",
    "GITHUB_REF_NAME": "feat/dev-images",
}
_MAIN_PUSH_ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_REF_NAME": "main",
}


class TestResolvePushMode:
    def test_flag_true_wins(self) -> None:
        env = {**_PR_ENV, "HYPERCI_PUBLISH_MODE": "true"}
        assert resolve_push_mode(dev_push=True, env=env) == PUBLISH

    def test_flag_dev_forces_dev_even_locally(self) -> None:
        # Explicit escape hatch for local / rehearsal use.
        assert resolve_push_mode(env={"HYPERCI_PUBLISH_MODE": "dev"}) == DEV

    def test_pr_with_opt_in_is_dev(self) -> None:
        env = {**_PR_ENV, "HYPERCI_PUBLISH_MODE": "false"}
        assert resolve_push_mode(dev_push=True, env=env) == DEV

    def test_pr_without_opt_in_is_validate(self) -> None:
        env = {**_PR_ENV, "HYPERCI_PUBLISH_MODE": "false"}
        assert resolve_push_mode(dev_push=False, env=env) == VALIDATE

    def test_branch_push_with_opt_in_is_dev(self) -> None:
        env = {**_BRANCH_PUSH_ENV, "HYPERCI_PUBLISH_MODE": "false"}
        assert resolve_push_mode(dev_push=True, env=env) == DEV

    def test_main_push_never_dev(self) -> None:
        # Validate-only main pushes stay validate even with the opt-in —
        # main's only pushing artifact class is the GA publish.
        env = {**_MAIN_PUSH_ENV, "HYPERCI_PUBLISH_MODE": "false"}
        assert resolve_push_mode(dev_push=True, env=env) == VALIDATE

    def test_local_run_never_auto_dev(self) -> None:
        # No GITHUB_ACTIONS: a laptop run must not push dev images unless
        # HYPERCI_PUBLISH_MODE=dev is set explicitly.
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/x"}
        assert resolve_push_mode(dev_push=True, env=env) == VALIDATE

    def test_legacy_fallback_dispatch_is_publish(self) -> None:
        # No flag at all (older workflows): workflow_dispatch == publish.
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch"}
        assert resolve_push_mode(env=env) == PUBLISH

    def test_explicit_false_dispatch_is_not_publish(self) -> None:
        # An explicit false must NOT fall through to the dispatch legacy
        # rule (pre-existing semantics, preserved).
        env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "HYPERCI_PUBLISH_MODE": "false",
        }
        assert resolve_push_mode(env=env) == VALIDATE

    def test_default_is_validate(self) -> None:
        assert resolve_push_mode(env={}) == VALIDATE


class TestBoolView:
    def test_is_publish_mode_true(self) -> None:
        assert is_publish_mode(env={"HYPERCI_PUBLISH_MODE": "true"}) is True

    def test_is_publish_mode_dev_is_not_publish(self) -> None:
        # helm / argocd treat dev as validate — never a publish.
        assert is_publish_mode(env={"HYPERCI_PUBLISH_MODE": "dev"}) is False


class TestBranchCiContext:
    def test_pr_event(self) -> None:
        assert is_branch_ci_context(env=_PR_ENV) is True

    def test_branch_push(self) -> None:
        assert is_branch_ci_context(env=_BRANCH_PUSH_ENV) is True

    def test_main_push(self) -> None:
        assert is_branch_ci_context(env=_MAIN_PUSH_ENV) is False

    def test_local(self) -> None:
        assert is_branch_ci_context(env={}) is False


class TestDevBranchSlug:
    def test_pr_head_ref_wins_over_merge_ref(self) -> None:
        # On pull_request GITHUB_REF_NAME is '7/merge' — useless as a tag.
        env = {**_PR_ENV, "GITHUB_REF_NAME": "7/merge"}
        assert dev_branch_slug(env=env) == "feat-dev-images"

    def test_slash_and_unicode_collapse(self) -> None:
        env = {"GITHUB_REF_NAME": "fix/plan job (permissions)!"}
        assert dev_branch_slug(env=env) == "fix-plan-job-permissions"

    def test_leading_invalid_chars_stripped(self) -> None:
        # A docker tag cannot start with '.' or '-'.
        env = {"GITHUB_REF_NAME": "--.-weird"}
        assert dev_branch_slug(env=env) == "weird"

    def test_length_capped(self) -> None:
        env = {"GITHUB_REF_NAME": "x" * 300}
        assert len(dev_branch_slug(env=env)) == 100

    def test_empty_when_no_ref(self) -> None:
        assert dev_branch_slug(env={}) == ""
