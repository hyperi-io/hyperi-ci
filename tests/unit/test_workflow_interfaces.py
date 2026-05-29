# Project:   HyperI CI
# File:      tests/unit/test_workflow_interfaces.py
# Purpose:   Tests for the reusable-workflow/composite interface compat gate
#
# License:   Proprietary — HYPERI PTY LIMITED
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
