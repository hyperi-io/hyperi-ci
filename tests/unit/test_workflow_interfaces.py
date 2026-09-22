# Project:   HyperI CI
# File:      tests/unit/test_workflow_interfaces.py
# Purpose:   Tests for the reusable-workflow/composite interface compat gate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Interface backward-compat gate (issue #31).

A consumer pins a caller (`python-ci.yml@<sha>`) but its siblings are written
`@main`, so the transitive graph floats. If a sibling's `workflow_call` /
composite interface regresses, the pinned caller's graph fails to compile at
startup (0 jobs). This gate fails hyperi-ci's own CI when an interface
regresses vs the last release, so the break never reaches a consumer.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_workflow_interfaces",
    Path(__file__).resolve().parents[2] / "scripts" / "check-workflow-interfaces.py",
)
assert _SPEC is not None and _SPEC.loader is not None  # always resolves for a real file
cwi = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cwi)


_REUSABLE = """
name: x
on:
  workflow_call:
    inputs:
      language:
        type: string
        required: true
      next-version:
        type: string
        default: ""
    secrets:
      TOKEN:
        required: true
    outputs:
      version:
        value: ${{ jobs.plan.outputs.v }}
"""

_COMPOSITE = """
name: setup
description: d
inputs:
  language:
    required: true
  python-version:
    required: false
    default: "3.12"
runs:
  using: composite
  steps: []
"""


class TestParseInterface:
    def test_parses_reusable_workflow(self) -> None:
        iface = cwi.parse_interface(_REUSABLE)
        assert iface["kind"] == "workflow"
        assert iface["inputs"]["language"]["required"] is True
        assert iface["inputs"]["next-version"]["required"] is False
        assert iface["inputs"]["next-version"]["has_default"] is True
        assert "TOKEN" in iface["secrets"]
        assert "version" in iface["outputs"]

    def test_parses_composite(self) -> None:
        iface = cwi.parse_interface(_COMPOSITE)
        assert iface["kind"] == "composite"
        assert iface["inputs"]["language"]["required"] is True
        assert iface["inputs"]["python-version"]["has_default"] is True
        assert iface["secrets"] == {}


class TestBreakingDeltas:
    def _wf(self, inputs=None, secrets=None, outputs=None) -> dict:
        return {
            "kind": "workflow",
            "inputs": inputs or {},
            "secrets": secrets or {},
            "outputs": set(outputs or []),
        }

    def test_no_change_is_clean(self) -> None:
        old = self._wf(inputs={"a": {"required": False, "has_default": True}})
        assert cwi.breaking_deltas(old, old) == []

    def test_added_optional_input_is_clean(self) -> None:
        old = self._wf(inputs={"a": {"required": False, "has_default": True}})
        new = self._wf(
            inputs={
                "a": {"required": False, "has_default": True},
                "b": {"required": False, "has_default": True},
            }
        )
        assert cwi.breaking_deltas(old, new) == []

    def test_new_required_input_is_breaking(self) -> None:
        old = self._wf()
        new = self._wf(inputs={"b": {"required": True, "has_default": False}})
        deltas = cwi.breaking_deltas(old, new)
        assert any("b" in d and "required" in d for d in deltas)

    def test_removed_input_is_breaking(self) -> None:
        old = self._wf(inputs={"a": {"required": False, "has_default": True}})
        new = self._wf()
        assert any("a" in d for d in cwi.breaking_deltas(old, new))

    def test_optional_to_required_is_breaking(self) -> None:
        old = self._wf(inputs={"a": {"required": False, "has_default": True}})
        new = self._wf(inputs={"a": {"required": True, "has_default": False}})
        assert any("a" in d for d in cwi.breaking_deltas(old, new))

    def test_removed_output_is_breaking(self) -> None:
        old = self._wf(outputs=["v"])
        new = self._wf()
        assert any("v" in d for d in cwi.breaking_deltas(old, new))

    def test_new_required_secret_is_breaking(self) -> None:
        old = self._wf()
        new = self._wf(secrets={"TOKEN": {"required": True}})
        assert any("TOKEN" in d for d in cwi.breaking_deltas(old, new))

    def test_removed_secret_is_breaking(self) -> None:
        old = self._wf(secrets={"TOKEN": {"required": True}})
        new = self._wf()
        assert any("TOKEN" in d for d in cwi.breaking_deltas(old, new))

    def test_new_optional_input_with_default_clean(self) -> None:
        old = self._wf()
        new = self._wf(inputs={"b": {"required": False, "has_default": True}})
        assert cwi.breaking_deltas(old, new) == []


class TestRemovedPipelineFiles:
    """A composite/workflow present at the last release but deleted now breaks
    a pinned caller's `@main` reference (404 at startup) — flag it."""

    def test_flags_deleted_file(self) -> None:
        old = {
            ".github/workflows/rust-ci.yml",
            ".github/actions/setup-runtime/action.yml",
        }
        cur = {".github/workflows/rust-ci.yml"}
        assert cwi.removed_pipeline_files(old, cur) == [
            ".github/actions/setup-runtime/action.yml"
        ]

    def test_none_when_all_present(self) -> None:
        s = {".github/workflows/rust-ci.yml"}
        assert cwi.removed_pipeline_files(s, s) == []

    def test_added_file_not_flagged(self) -> None:
        old = {".github/workflows/rust-ci.yml"}
        cur = {".github/workflows/rust-ci.yml", ".github/workflows/new.yml"}
        assert cwi.removed_pipeline_files(old, cur) == []


class TestTheCliSubcommandGate:
    """A workflow may only call a subcommand the PUBLISHED CLI already has.

    Workflows float `@main` and reach a consumer instantly; the CLI arrives
    only on a release. A subcommand added in the same commit as its caller is
    therefore missing on every runner until the next publish (issue #181).
    """

    def test_a_missing_subcommand_is_reported(self) -> None:
        gaps = cwi.cli_command_gaps({"a.yml": {"run", "gate-check"}}, {"run"})
        assert len(gaps) == 1
        assert "gate-check" in gaps[0]
        assert "a.yml" in gaps[0]

    def test_a_published_subcommand_is_not_reported(self) -> None:
        assert cwi.cli_command_gaps({"a.yml": {"run", "watch"}}, {"run", "watch"}) == []

    def test_every_workflow_is_named(self) -> None:
        gaps = cwi.cli_command_gaps({"a.yml": {"new"}, "b.yml": {"new"}}, set())
        assert len(gaps) == 2

    def test_a_hidden_command_counts_as_published(self) -> None:
        """`--help` hides some commands, so the enumeration must not scrape it."""
        assert cwi.cli_command_gaps({"a.yml": {"tag-head"}}, {"tag-head"}) == []
