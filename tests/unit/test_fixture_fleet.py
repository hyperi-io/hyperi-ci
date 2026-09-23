# Project:   HyperI CI
# File:      tests/unit/test_fixture_fleet.py
# Purpose:   Tests for the ci-test-* fleet SSoT and its consistency gate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Fixture-fleet SSoT tests.

The fleet lived in CLAUDE.md prose, untracked, and said 8 while the org had 9.
A list in prose cannot be checked against anything, so the point of the SSoT
is the check that reads it -- and a check has three outcomes here, not two:
matches, drifted, and could-not-ask.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import fixture_fleet  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "check_fixture_fleet", _ROOT / "scripts" / "check-fixture-fleet.py"
)
assert _SPEC is not None and _SPEC.loader is not None  # a real file always resolves
cff = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cff)


class TestTheSsotItself:
    def test_every_entry_has_a_name_and_a_language(self) -> None:
        data = yaml.safe_load((_ROOT / "config" / "fixtures.yaml").read_text("utf-8"))
        for entry in data["fleet"]:
            assert entry["name"].startswith("ci-test-"), entry
            assert entry["language"], entry
            assert entry["covers"], entry

    def test_names_are_unique(self) -> None:
        names = [e["name"] for e in cff_fleet()]
        assert len(names) == len(set(names))

    def test_names_are_the_upstream_repo_not_a_local_directory(self) -> None:
        """Four local checkout dirs have drifted from their repo names, and the
        repo name is the authority -- `gh --repo` takes nothing else."""
        drifted = {
            "ci-test-go-simple",
            "ci-test-python-cli",
            "ci-test-python-package",
            "ci-test-rust-minimal",
        }
        assert drifted.isdisjoint(cff.declared())


class TestTheCheckHasThreeOutcomes:
    """Matches, drifted, and could-not-ask. Collapsing the third would report
    'every fixture was deleted' on a network blip."""

    def test_a_matching_fleet_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cff, "actual", cff.declared)
        assert cff.main() == 0

    def test_a_deleted_repo_is_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cff, "actual", lambda: cff.declared() - {"ci-test-rust-lib"}
        )
        assert cff.main() == 1

    def test_an_undeclared_repo_is_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fixture nobody declared is never swept, which is the quiet half."""
        monkeypatch.setattr(
            cff, "actual", lambda: cff.declared() | {"ci-test-brand-new"}
        )
        assert cff.main() == 1

    def test_an_unreachable_org_is_neither(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cff, "actual", lambda: None)
        assert cff.main() == 2

    def test_a_failing_gh_returns_none_rather_than_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty set and an unreachable org must not be the same value."""
        monkeypatch.setattr(
            cff.subprocess,
            "run",
            lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})(),
        )
        assert cff.actual() is None

    def test_malformed_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cff.subprocess,
            "run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "not json"})(),
        )
        assert cff.actual() is None


class TestTheSsotDrivesSelection:
    """The path -> fixture mapping is data in the SSoT, not a table in code."""

    def test_every_entry_names_a_workflow_that_exists(self) -> None:
        present = {p.name for p in (_ROOT / ".github" / "workflows").glob("*.yml")}
        for entry in cff_fleet():
            assert entry["workflow"] in present, entry

    def test_exactly_one_canary(self) -> None:
        canaries = [e for e in cff_fleet() if e.get("rehearsal") == "canary"]
        assert len(canaries) == 1, canaries

    def test_every_workflow_has_exactly_one_rehearsal_target(self) -> None:
        fleet = cff_fleet()
        for workflow in fixture_fleet.language_workflows(fleet):
            targets = [
                e
                for e in fleet
                if e["workflow"] == workflow
                and e.get("rehearsal") in ("canary", "default")
            ]
            assert len(targets) == 1, (workflow, targets)

    def test_rehearsal_roles_are_spelled_right(self) -> None:
        """A typo'd role silently drops a workflow's only rehearsal target."""
        for entry in cff_fleet():
            assert entry.get("rehearsal", "default") in ("canary", "default"), entry


class TestMasksCarryTheirWayOut:
    """A fixture that switches a hyperi-ci feature off stops the fleet testing
    it, and without an issue number nothing ever turns it back on."""

    def test_the_declared_masks_are_complete(self) -> None:
        assert fixture_fleet.mask_problems(cff_fleet()) == []

    def test_a_mask_without_an_issue_is_a_problem(self) -> None:
        fleet = [{"name": "ci-test-x", "masks": [{"feature": "f", "why": "w"}]}]
        assert any("issue" in p for p in fixture_fleet.mask_problems(fleet))

    def test_an_issue_that_is_not_a_number_is_a_problem(self) -> None:
        fleet = [
            {
                "name": "ci-test-x",
                "masks": [{"feature": "f", "why": "w", "issue": "soon"}],
            }
        ]
        assert any("issue" in p for p in fixture_fleet.mask_problems(fleet))

    def test_a_mask_without_a_reason_is_a_problem(self) -> None:
        fleet = [{"name": "ci-test-x", "masks": [{"feature": "f", "issue": 1}]}]
        assert any("why" in p for p in fixture_fleet.mask_problems(fleet))

    def test_every_mask_is_printed(self) -> None:
        lines = fixture_fleet.mask_lines(cff_fleet())
        assert len(lines) == len(fixture_fleet.masks(cff_fleet()))
        assert all("hyperi-io/hyperi-ci#" in line for line in lines)


class TestSelectingWhatToRehearse:
    FLEET = [
        {"name": "ci-test-go-app", "workflow": "go-ci.yml", "rehearsal": "canary"},
        {"name": "ci-test-py-app", "workflow": "python-ci.yml", "rehearsal": "default"},
        {"name": "ci-test-py-lib", "workflow": "python-ci.yml"},
        {"name": "ci-test-rs-app", "workflow": "rust-ci.yml", "rehearsal": "default"},
    ]
    TEXTS = {
        "go-ci.yml": "uses: hyperi-io/hyperi-ci/.github/actions/predict-version@main",
        "python-ci.yml": "uses: hyperi-io/hyperi-ci/.github/actions/predict-version@main",
        "rust-ci.yml": (
            "uses: hyperi-io/hyperi-ci/.github/actions/predict-version@main\n"
            "uses: hyperi-io/hyperi-ci/.github/actions/setup-rust-tools@main"
        ),
        "_release-tail.yml": (
            "uses: hyperi-io/hyperi-ci/.github/actions/setup-semantic-release@main"
        ),
    }

    def _select(self, *paths: str) -> list[str]:
        chosen = fixture_fleet.select_for_paths(list(paths), self.FLEET, self.TEXTS)
        return [entry["name"] for entry in chosen]

    def test_a_language_workflow_selects_its_own_target(self) -> None:
        assert self._select(".github/workflows/rust-ci.yml") == ["ci-test-rs-app"]

    def test_a_shared_workflow_selects_the_canary(self) -> None:
        assert self._select(".github/workflows/_release-tail.yml") == ["ci-test-go-app"]

    def test_an_action_every_language_calls_selects_the_canary(self) -> None:
        assert self._select(".github/actions/predict-version/action.yml") == [
            "ci-test-go-app"
        ]

    def test_an_action_one_language_calls_selects_that_language(self) -> None:
        assert self._select(".github/actions/setup-rust-tools/action.yml") == [
            "ci-test-rs-app"
        ]

    def test_an_action_only_the_shared_tail_calls_selects_the_canary(self) -> None:
        assert self._select(".github/actions/setup-semantic-release/action.yml") == [
            "ci-test-go-app"
        ]

    def test_a_non_consumer_workflow_selects_nothing(self) -> None:
        """hyperi-ci's own CI and its audits are not on anyone's @main path."""
        assert self._select(".github/workflows/versions-audit.yml") == []

    def test_source_and_docs_select_nothing(self) -> None:
        assert self._select("src/hyperi_ci/cli.py", "docs/lessons.md") == []

    def test_two_changes_select_both_without_duplicates(self) -> None:
        assert self._select(
            ".github/workflows/rust-ci.yml",
            ".github/workflows/python-ci.yml",
            ".github/workflows/_release-tail.yml",
            ".github/actions/setup-rust-tools/action.yml",
        ) == ["ci-test-go-app", "ci-test-py-app", "ci-test-rs-app"]

    def test_a_non_target_fixture_is_never_selected(self) -> None:
        """python-ci.yml has three fixtures; a PR rehearses ONE of them."""
        assert "ci-test-py-lib" not in self._select(".github/workflows/python-ci.yml")

    def test_the_real_fleet_and_workflows_resolve(self) -> None:
        chosen = fixture_fleet.select_for_paths(
            [".github/workflows/rust-ci.yml"],
            cff_fleet(),
            fixture_fleet.read_workflow_texts(),
        )
        assert [e["name"] for e in chosen] == ["ci-test-rust-app"]


def cff_fleet() -> list[dict]:
    """The raw fleet entries from the SSoT."""
    data = yaml.safe_load((_ROOT / "config" / "fixtures.yaml").read_text("utf-8"))
    return data["fleet"]
