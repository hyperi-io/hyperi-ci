# Project:   HyperI CI
# File:      tests/unit/test_trigger.py
# Purpose:   Dispatch-input parsing and the argv trigger hands to gh
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for workflow_dispatch inputs (issue #97).

``trigger`` sent only ``--ref``, so a workflow declaring required inputs
could not be dispatched through the wrapper at all.
"""

from __future__ import annotations

import subprocess

import pytest

from hyperi_ci import trigger


class TestParseInputs:
    """`key=value` pairs become the dict trigger_workflow sends."""

    def test_none_is_empty(self) -> None:
        assert trigger.parse_inputs(None) == {}

    def test_empty_list_is_empty(self) -> None:
        assert trigger.parse_inputs([]) == {}

    def test_one_pair(self) -> None:
        assert trigger.parse_inputs(["open_pr=true"]) == {"open_pr": "true"}

    def test_several_keep_their_order(self) -> None:
        parsed = trigger.parse_inputs(
            ["upstream_ref=v1.2.3", "base_ref=main", "open_pr=false"]
        )
        assert list(parsed) == ["upstream_ref", "base_ref", "open_pr"]

    def test_only_the_first_equals_separates(self) -> None:
        assert trigger.parse_inputs(["expr=a=b"]) == {"expr": "a=b"}

    def test_an_empty_value_is_allowed(self) -> None:
        assert trigger.parse_inputs(["tag="]) == {"tag": ""}

    def test_space_around_the_key_is_dropped(self) -> None:
        assert trigger.parse_inputs([" tag =v1"]) == {"tag": "v1"}

    @pytest.mark.parametrize("bad", ["justakey", "=novalue", " =x"])
    def test_a_malformed_entry_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="key=value"):
            trigger.parse_inputs([bad])


class TestTriggerArgv:
    """Each input reaches gh as its own -f flag."""

    @staticmethod
    def _capture(
        monkeypatch: pytest.MonkeyPatch,
        *,
        workflow: str = "ci.yml",
        inputs: dict[str, str] | None = None,
    ) -> list[str]:
        sent: list[str] = []

        def fake_gh_run(args: list[str], **_kw: object) -> subprocess.CompletedProcess:
            sent.extend(args)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(trigger, "require_gh", lambda: True)
        monkeypatch.setattr(trigger, "get_current_branch", lambda: "main")
        monkeypatch.setattr(trigger, "gh_run", fake_gh_run)
        trigger.trigger_workflow(workflow=workflow, inputs=inputs)
        return sent

    def test_no_inputs_sends_only_the_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._capture(monkeypatch)
        assert sent == ["workflow", "run", "ci.yml", "--ref", "main"]

    def test_each_input_gets_its_own_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._capture(
            monkeypatch,
            workflow="upstream-sync.yml",
            inputs={"upstream_ref": "v1.2.3", "open_pr": "true"},
        )
        assert sent.count("-f") == 2
        assert "upstream_ref=v1.2.3" in sent
        assert "open_pr=true" in sent

    def test_the_ref_still_leads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._capture(monkeypatch, inputs={"a": "b"})
        assert sent[:5] == ["workflow", "run", "ci.yml", "--ref", "main"]
