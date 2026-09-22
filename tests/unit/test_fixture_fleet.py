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
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
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


def cff_fleet() -> list[dict]:
    """The raw fleet entries from the SSoT."""
    data = yaml.safe_load((_ROOT / "config" / "fixtures.yaml").read_text("utf-8"))
    return data["fleet"]
