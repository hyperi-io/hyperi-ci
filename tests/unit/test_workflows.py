# Project:   HyperI CI
# File:      tests/unit/test_workflows.py
# Purpose:   Workflow inventory and the ownership reading light touch uses
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the workflow inventory (issue #97).

Ownership is read from the file, not declared: a workflow is hyperi-ci's
when a job delegates to a hyperi-io/hyperi-ci reusable workflow. The
fixtures below are the real shapes from hyperi-io/dfe-hyperdx, which
carries a scaffolded ci.yml alongside six it wrote itself.
"""

from __future__ import annotations

from pathlib import Path

from hyperi_ci import workflows

# dfe-hyperdx's ci.yml, trimmed to the job that carries the marker.
_SCAFFOLDED = """\
name: CI

'on':
  push:
    branches: ['**']
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: hyperi-io/hyperi-ci/.github/workflows/ts-ci.yml@main
    secrets: inherit
"""

# dfe-hyperdx's upstream-sync.yml: bespoke, and the one that takes the
# three dispatch inputs issue #97 could not express.
_BESPOKE = """\
name: upstream-sync

'on':
  workflow_dispatch:
    inputs:
      upstream_ref:
        type: string
        required: true
      base_ref:
        type: string
        required: false
      open_pr:
        type: boolean
        default: true
  schedule:
    - cron: '0 3 * * 1'

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""


def _repo(root: Path, files: dict[str, str]) -> Path:
    """Write a .github/workflows tree and return the repo root."""
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True)
    for name, body in files.items():
        (directory / name).write_text(body, encoding="utf-8")
    return root


class TestOwnership:
    """A workflow is ours only when it calls our reusable workflow."""

    def test_a_scaffolded_ci_is_owned(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED})
        (found,) = workflows.inventory(tmp_path)
        assert found.owned is True
        assert found.name == "CI"
        assert found.filename == "ci.yml"

    def test_a_bespoke_workflow_is_not_owned(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"upstream-sync.yml": _BESPOKE})
        (found,) = workflows.inventory(tmp_path)
        assert found.owned is False
        assert found.name == "upstream-sync"

    def test_both_readings_hold_of_one_repo(self, tmp_path: Path) -> None:
        # The dfe-hyperdx shape: our ci.yml beside workflows we must not own.
        _repo(
            tmp_path,
            {
                "ci.yml": _SCAFFOLDED,
                "upstream-sync.yml": _BESPOKE,
                "fork-security.yml": _BESPOKE.replace("upstream-sync", "fork-security"),
            },
        )
        inventory = workflows.inventory(tmp_path)
        assert workflows.owned_names(inventory) == ["CI"]
        assert sorted(wf.name for wf in inventory if not wf.owned) == [
            "fork-security",
            "upstream-sync",
        ]

    def test_a_self_hosted_local_call_is_not_owned(self, tmp_path: Path) -> None:
        # hyperi-ci's own ci.yml calls ./.github/workflows/_release-tail.yml,
        # which is not the consumer-scaffold marker.
        body = _SCAFFOLDED.replace(
            "hyperi-io/hyperi-ci/.github/workflows/ts-ci.yml@main",
            "./.github/workflows/_release-tail.yml",
        )
        _repo(tmp_path, {"ci.yml": body})
        (found,) = workflows.inventory(tmp_path)
        assert found.owned is False

    def test_a_matching_string_outside_a_uses_key_is_not_ownership(
        self, tmp_path: Path
    ) -> None:
        # A comment naming the reusable workflow must not claim the file.
        body = _BESPOKE.replace(
            "jobs:",
            "# not hyperi-io/hyperi-ci/.github/workflows/ts-ci.yml\njobs:",
        )
        _repo(tmp_path, {"upstream-drift.yml": body})
        (found,) = workflows.inventory(tmp_path)
        assert found.owned is False

    def test_unparseable_yaml_still_appears(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"broken.yml": "name: [unclosed\n"})
        (found,) = workflows.inventory(tmp_path)
        assert found.filename == "broken.yml"
        assert found.name == ".github/workflows/broken.yml"
        assert found.owned is False

    def test_a_nameless_workflow_reads_as_its_path(self, tmp_path: Path) -> None:
        # GitHub reports the path as workflowName when the file names nothing.
        _repo(tmp_path, {"audit.yml": "'on':\n  push:\n"})
        (found,) = workflows.inventory(tmp_path)
        assert found.name == ".github/workflows/audit.yml"


class TestInventory:
    """Reading the directory."""

    def test_no_workflows_directory_is_empty(self, tmp_path: Path) -> None:
        assert workflows.inventory(tmp_path) == []

    def test_non_workflow_files_are_skipped(self, tmp_path: Path) -> None:
        root = _repo(tmp_path, {"ci.yml": _SCAFFOLDED})
        (root / ".github" / "workflows" / "README.md").write_text("x", encoding="utf-8")
        assert [wf.filename for wf in workflows.inventory(root)] == ["ci.yml"]

    def test_yaml_suffix_counts_too(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"ci.yaml": _SCAFFOLDED})
        assert [wf.filename for wf in workflows.inventory(tmp_path)] == ["ci.yaml"]


class TestFind:
    """A workflow is named by filename, stem or display name."""

    def _inventory(self, tmp_path: Path) -> list[workflows.Workflow]:
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED, "upstream-sync.yml": _BESPOKE})
        return workflows.inventory(tmp_path)

    def test_by_filename(self, tmp_path: Path) -> None:
        found = workflows.find(self._inventory(tmp_path), "upstream-sync.yml")
        assert found is not None
        assert found.filename == "upstream-sync.yml"

    def test_by_stem(self, tmp_path: Path) -> None:
        # What a run listing shows, and what a human types.
        found = workflows.find(self._inventory(tmp_path), "upstream-sync")
        assert found is not None
        assert found.filename == "upstream-sync.yml"

    def test_by_display_name_case_insensitively(self, tmp_path: Path) -> None:
        found = workflows.find(self._inventory(tmp_path), "ci")
        assert found is not None
        assert found.filename == "ci.yml"

    def test_unknown_token_is_none(self, tmp_path: Path) -> None:
        assert workflows.find(self._inventory(tmp_path), "nope") is None

    def test_empty_token_is_none(self, tmp_path: Path) -> None:
        assert workflows.find(self._inventory(tmp_path), "  ") is None


class TestOwnedNames:
    """What the stand-down reads to mark a workflow as not ours."""

    def test_only_scaffolded_workflows_are_named(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"ci.yml": _SCAFFOLDED, "upstream-sync.yml": _BESPOKE})
        assert workflows.owned_names(workflows.inventory(tmp_path)) == ["CI"]

    def test_a_repo_of_its_own_workflows_owns_none(self, tmp_path: Path) -> None:
        _repo(tmp_path, {"upstream-sync.yml": _BESPOKE})
        assert workflows.owned_names(workflows.inventory(tmp_path)) == []
