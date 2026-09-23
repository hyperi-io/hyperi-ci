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

import importlib.util
from pathlib import Path
from typing import Self

import pytest

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


class TestWhatCountsAsPublished:
    """uv answers "what is the latest" from a cached index, PyPI does not."""

    def test_an_unreachable_pypi_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None is the skip path; a wrong version would fail a clean PR."""

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("no route to host")

        monkeypatch.setattr(cwi.urllib.request, "urlopen", _boom)
        assert cwi.latest_published_version() is None

    def test_a_malformed_payload_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            cwi.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(b"{}")
        )
        assert cwi.latest_published_version() is None

    def test_the_version_comes_from_pypi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b'{"info": {"version": "9.9.9"}}'
        monkeypatch.setattr(
            cwi.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        assert cwi.latest_published_version() == "9.9.9"

    def test_an_unresolvable_version_skips_rather_than_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cwi, "latest_published_version", lambda: None)
        assert cwi.published_cli_commands() is None


_RETIREMENT = """
retired:
  - file: .github/workflows/go-ci.yml
    kind: secret
    name: JFROG_TOKEN
    reason: Nothing reads it and no caller passes it.
    checked: 2026-09-23
"""


def _entry(**overrides: object) -> str:
    """A retirement file with one entry; a None override drops that field."""
    fields: dict[str, object] = {
        "file": ".github/workflows/go-ci.yml",
        "kind": "secret",
        "name": "JFROG_TOKEN",
        "reason": "Nothing reads it and no caller passes it.",
        "checked": "2026-09-23",
    }
    fields.update(overrides)
    body = "\n".join(f"    {k}: {v}" for k, v in fields.items() if v is not None)
    return f"retired:\n  -\n{body}\n"


class TestRetirementRecords:
    """An interface nothing consumes must be removable without a major bump.

    The gate compares interface to interface, so every removal reads as a
    regression. A record clears one, and carries the evidence that earned it.
    """

    def test_a_listed_removal_is_not_a_regression(self) -> None:
        old = {
            "kind": "workflow",
            "inputs": {},
            "secrets": {"JFROG_TOKEN": {"required": False}},
            "outputs": set(),
        }
        new = {"kind": "workflow", "inputs": {}, "secrets": {}, "outputs": set()}
        retired = frozenset({("secret", "JFROG_TOKEN")})
        assert cwi.breaking_deltas(old, new, retired) == []

    def test_an_unlisted_removal_in_the_same_file_still_fails(self) -> None:
        """Clearing one member must not clear its neighbours."""
        old = {
            "kind": "workflow",
            "inputs": {},
            "secrets": {
                "JFROG_TOKEN": {"required": False},
                "NPM_TOKEN": {"required": False},
            },
            "outputs": set(),
        }
        new = {"kind": "workflow", "inputs": {}, "secrets": {}, "outputs": set()}
        deltas = cwi.breaking_deltas(old, new, frozenset({("secret", "JFROG_TOKEN")}))
        assert deltas == ["secret 'NPM_TOKEN' removed"]

    def test_a_record_does_not_clear_a_different_kind(self) -> None:
        """A retired secret must not excuse an input of the same name."""
        old = {
            "kind": "workflow",
            "inputs": {"TOKEN": {"required": False, "has_default": True}},
            "secrets": {},
            "outputs": set(),
        }
        new = {"kind": "workflow", "inputs": {}, "secrets": {}, "outputs": set()}
        deltas = cwi.breaking_deltas(old, new, frozenset({("secret", "TOKEN")}))
        assert len(deltas) == 1
        assert "input 'TOKEN'" in deltas[0]

    def test_a_valid_record_parses(self) -> None:
        records = cwi.parse_retirements(_RETIREMENT)
        assert len(records) == 1
        assert records[0].file == ".github/workflows/go-ci.yml"
        assert records[0].member == ("secret", "JFROG_TOKEN")
        assert records[0].checked == "2026-09-23"

    @pytest.mark.parametrize("field", ["reason", "checked", "file", "kind", "name"])
    def test_a_record_missing_a_field_is_rejected(self, field: str) -> None:
        """No reason means no evidence, which is a bypass, not a retirement."""
        with pytest.raises(ValueError, match=field):
            cwi.parse_retirements(_entry(**{field: None}))

    def test_an_empty_reason_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            cwi.parse_retirements(_entry(reason='"   "'))

    def test_an_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="parameter"):
            cwi.parse_retirements(_entry(kind="parameter"))

    def test_an_unknown_field_is_rejected(self) -> None:
        """Catches a typo that would otherwise drop the field it meant."""
        with pytest.raises(ValueError, match="resaon"):
            cwi.parse_retirements(_entry(resaon="typo"))

    def test_a_non_list_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="list"):
            cwi.parse_retirements("retired: {}")

    def test_an_absent_file_means_no_retirements(self, tmp_path: Path) -> None:
        """Checking everything is the safe direction to fail in."""
        assert cwi.load_retirements(tmp_path / "nope.yaml") == []


class TestRetirementState:
    """Every record is reported, so a gate never quietly stops checking."""

    def _record(self) -> object:
        return cwi.Retirement(
            file=".github/workflows/go-ci.yml",
            kind="secret",
            name="JFROG_TOKEN",
            reason="Nothing reads it.",
            checked="2026-09-23",
        )

    def _iface(self, *secrets: str) -> dict:
        return {
            "kind": "workflow",
            "inputs": {},
            "secrets": {s: {"required": False} for s in secrets},
            "outputs": set(),
        }

    def test_still_declared_is_pending(self) -> None:
        state = cwi.retirement_state(
            self._record(), self._iface("JFROG_TOKEN"), self._iface("JFROG_TOKEN")
        )
        assert state == "pending"

    def test_gone_from_the_tree_only_is_retired(self) -> None:
        state = cwi.retirement_state(
            self._record(), self._iface(), self._iface("JFROG_TOKEN")
        )
        assert state == "retired"

    def test_gone_from_both_is_prunable(self) -> None:
        """Once a release ships without it the record stops doing anything."""
        state = cwi.retirement_state(self._record(), self._iface(), self._iface())
        assert state == "prunable"

    def test_a_file_with_no_interface_counts_as_absent(self) -> None:
        assert cwi.retirement_state(self._record(), None, None) == "prunable"


class TestTheShippedRetirementFile:
    """The repo's own records must parse and point at real declarations."""

    def test_it_parses(self) -> None:
        records = cwi.load_retirements(cwi._RETIREMENTS)
        assert records, "expected the JFrog retirements"

    def test_every_record_names_a_file_that_exists(self) -> None:
        """A typo'd path reads as prunable, which would hide the mistake."""
        root = Path(cwi._ROOT)
        for record in cwi.load_retirements(cwi._RETIREMENTS):
            assert (root / record.file).is_file(), record.file

    def test_no_record_is_still_declared(self) -> None:
        """A pending record pre-authorises a removal nobody has made yet."""
        root = Path(cwi._ROOT)
        for record in cwi.load_retirements(cwi._RETIREMENTS):
            iface = cwi.parse_interface(
                (root / record.file).read_text(encoding="utf-8")
            )
            assert not cwi._declares(iface, record.kind, record.name), record.name


class _FakeResponse:
    """Context-manager stand-in for what `urlopen` returns."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body
