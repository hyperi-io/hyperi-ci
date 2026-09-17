# Project:   HyperI CI
# File:      tests/unit/test_rerun.py
# Purpose:   Run selection and the argv rerun hands to gh
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the rerun wrapper (issue #97).

``gh run rerun --failed`` had no wrapper, so telling a flake from a real
failure meant the native CLI or pushing an empty commit.
"""

from __future__ import annotations

import subprocess

import pytest

from hyperi_ci import rerun
from hyperi_ci.gh import RunSelectionError


def _recorder(sent: list[str]):
    """A gh_run stand-in that records the argv it was handed."""

    def fake_gh_run(args: list[str], **_kw: object) -> subprocess.CompletedProcess:
        sent.extend(args)
        return subprocess.CompletedProcess([], 0, "", "")

    return fake_gh_run


class TestRerunArgv:
    """The flags rerun hands to gh."""

    @staticmethod
    def _capture(
        monkeypatch: pytest.MonkeyPatch, **kwargs: object
    ) -> tuple[int, list[str]]:
        sent: list[str] = []
        monkeypatch.setattr(rerun, "require_gh", lambda: True)
        monkeypatch.setattr(rerun, "gh_run", _recorder(sent))
        rc = rerun.rerun_run(**kwargs)  # type: ignore[arg-type]
        return rc, sent

    def test_failed_only_is_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, sent = self._capture(monkeypatch, run_id="123")
        assert rc == 0
        assert sent == ["run", "rerun", "123", "--failed"]

    def test_all_jobs_drops_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, sent = self._capture(monkeypatch, run_id="123", failed_only=False)
        assert rc == 0
        assert sent == ["run", "rerun", "123"]

    def test_repo_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, sent = self._capture(monkeypatch, run_id="123", repo="hyperi-io/dfe-loader")
        assert sent[sent.index("--repo") + 1] == "hyperi-io/dfe-loader"


class TestRunSelection:
    """Selection matches watch: HEAD's own run, ambiguity refused."""

    def test_repo_without_a_run_id_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[str] = []
        monkeypatch.setattr(rerun, "require_gh", lambda: True)
        monkeypatch.setattr(rerun, "gh_run", _recorder(sent))
        # HEAD says nothing about another repo, so this must not reach gh.
        assert rerun.rerun_run(repo="hyperi-io/dfe-loader") == 1
        assert sent == []

    def test_head_run_is_resolved_when_no_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[str] = []
        monkeypatch.setattr(rerun, "require_gh", lambda: True)
        monkeypatch.setattr(rerun, "resolve_head_run", lambda **_k: {"databaseId": 99})
        monkeypatch.setattr(rerun, "describe_run", lambda _run: "99 CI")
        monkeypatch.setattr(rerun, "gh_run", _recorder(sent))
        assert rerun.rerun_run() == 0
        assert sent == ["run", "rerun", "99", "--failed"]

    def test_an_ambiguous_head_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(**_kw: object) -> dict:
            raise RunSelectionError("several runs match")

        monkeypatch.setattr(rerun, "require_gh", lambda: True)
        monkeypatch.setattr(rerun, "resolve_head_run", boom)
        assert rerun.rerun_run() == 1

    def test_no_gh_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rerun, "require_gh", lambda: False)
        assert rerun.rerun_run(run_id="123") == 1


class TestDispatchFailure:
    """A gh failure is reported, not swallowed."""

    def test_a_failed_dispatch_returns_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(args: list[str], **_kw: object) -> subprocess.CompletedProcess:
            raise subprocess.CalledProcessError(1, args)

        monkeypatch.setattr(rerun, "require_gh", lambda: True)
        monkeypatch.setattr(rerun, "gh_run", boom)
        assert rerun.rerun_run(run_id="123") == 1
