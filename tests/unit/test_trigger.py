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
from pathlib import Path

import pytest

from hyperi_ci import trigger

# dfe-hyperdx's shape: our scaffolded ci.yml beside a workflow it wrote.
_SCAFFOLDED = (
    "name: CI\n'on':\n  push:\njobs:\n  ci:\n"
    "    uses: hyperi-io/hyperi-ci/.github/workflows/ts-ci.yml@main\n"
)
_BESPOKE = "name: upstream-sync\n'on':\n  workflow_dispatch:\n"


def _repo(root: Path) -> Path:
    """Write a .github/workflows tree and return the repo root."""
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yml").write_text(_SCAFFOLDED, encoding="utf-8")
    (directory / "upstream-sync.yml").write_text(_BESPOKE, encoding="utf-8")
    return root


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
        repo: str | None = None,
        project_dir: Path | None = None,
    ) -> list[str]:
        sent: list[str] = []

        def fake_gh_run(args: list[str], **_kw: object) -> subprocess.CompletedProcess:
            sent.extend(args)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(trigger, "require_gh", lambda: True)
        monkeypatch.setattr(trigger, "get_current_branch", lambda: "main")
        monkeypatch.setattr(trigger, "gh_run", fake_gh_run)
        trigger.trigger_workflow(
            workflow=workflow,
            inputs=inputs,
            repo=repo,
            project_dir=project_dir,
        )
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

    def test_repo_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._capture(monkeypatch, repo="hyperi-io/ci-test-python-lib")
        assert sent[sent.index("--repo") + 1] == "hyperi-io/ci-test-python-lib"


class TestWorkflowResolution:
    """Any workflow the repo carries, named however the caller has it."""

    def test_a_bespoke_stem_resolves_to_its_file(self, tmp_path: Path) -> None:
        # `-w upstream-sync` is what a run listing shows, not the filename.
        root = _repo(tmp_path)
        assert (
            trigger.resolve_workflow_file("upstream-sync", root) == "upstream-sync.yml"
        )

    def test_a_display_name_resolves(self, tmp_path: Path) -> None:
        assert trigger.resolve_workflow_file("CI", _repo(tmp_path)) == "ci.yml"

    def test_a_filename_is_kept(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        assert (
            trigger.resolve_workflow_file("upstream-sync.yml", root)
            == "upstream-sync.yml"
        )

    def test_an_unknown_token_passes_through(self, tmp_path: Path) -> None:
        # Refusing off a local listing would fail closed on a checkout
        # that lags the remote; gh answers instead.
        root = _repo(tmp_path)
        assert trigger.resolve_workflow_file("release.yml", root) == "release.yml"

    def test_no_workflows_directory_passes_through(self, tmp_path: Path) -> None:
        assert trigger.resolve_workflow_file("ci.yml", tmp_path) == "ci.yml"

    def test_a_bespoke_workflow_reaches_gh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The dfe-hyperdx case, end to end: three inputs on a workflow
        # hyperi-ci did not scaffold.
        sent = TestTriggerArgv._capture(
            monkeypatch,
            workflow="upstream-sync",
            inputs={
                "upstream_ref": "v2.4.0",
                "base_ref": "main",
                "open_pr": "true",
            },
            project_dir=_repo(tmp_path),
        )
        assert sent[:5] == ["workflow", "run", "upstream-sync.yml", "--ref", "main"]
        assert sent.count("-f") == 3
        assert "upstream_ref=v2.4.0" in sent
        assert "base_ref=main" in sent
        assert "open_pr=true" in sent

    def test_a_named_repo_skips_the_local_inventory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # This checkout's workflow names say nothing about another repo.
        sent = TestTriggerArgv._capture(
            monkeypatch,
            workflow="CI",
            repo="hyperi-io/dfe-hyperdx",
            project_dir=_repo(tmp_path),
        )
        assert sent[2] == "CI"
