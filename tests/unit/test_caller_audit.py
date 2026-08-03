# Project:   HyperI CI
# File:      tests/unit/test_caller_audit.py
# Purpose:   Consumer ci.yml drift against the dispatch contract (issue #88)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Caller-audit tests.

The shapes here are taken from real consumers: `COMPLIANT` is dfe-receiver
after its fix, `PRE_FIX` is the shape that produced the HTTP 422 across four
Rust repos.
"""

from __future__ import annotations

import pytest

from hyperi_ci.caller_audit import audit_local, audit_text
from hyperi_ci.publish.dispatch import DISPATCH_INPUTS, _dispatch_cmd

COMPLIANT = """
name: CI
on:
  push:
    branches: ["**"]
  workflow_dispatch:
    inputs:
      tag:
        type: string
        required: false
        default: ""
      from-head:
        type: string
        required: false
        default: ""
      bump:
        type: string
        required: false
        default: "auto"
jobs:
  ci:
    uses: hyperi-io/hyperi-ci/.github/workflows/rust-ci.yml@main
    with:
      publish-target: both
      tag: ${{ inputs.tag || '' }}
      from-head: ${{ inputs.from-head || '' }}
      bump: ${{ inputs.bump || 'auto' }}
    secrets: inherit
"""

PRE_FIX = """
name: CI
on:
  workflow_dispatch:
    inputs:
      tag:
        type: string
        required: true
jobs:
  ci:
    uses: hyperi-io/hyperi-ci/.github/workflows/rust-ci.yml@main
    with:
      publish-target: both
"""


def _kinds(text: str) -> set[tuple[str, str]]:
    report = audit_text("under-test", text)
    return {(f.kind, f.input_name) for f in report.findings}


def test_compliant_caller_is_clean() -> None:
    report = audit_text("dfe-receiver", COMPLIANT)
    assert report.ok
    assert report.calls is not None
    assert report.declared == ["bump", "from-head", "tag"]
    assert report.forwarded == ["bump", "from-head", "tag"]


def test_pre_fix_caller_reports_every_fault() -> None:
    """The exact shape that 422'd four Rust repos."""
    assert _kinds(PRE_FIX) == {
        ("missing", "from-head"),
        ("missing", "bump"),
        ("not-forwarded", "tag"),
        ("required", "tag"),
    }


def test_declared_but_not_forwarded_is_reported() -> None:
    """An input accepted then silently ignored is worse than the 422."""
    text = COMPLIANT.replace("      bump: ${{ inputs.bump || 'auto' }}\n", "")
    assert ("not-forwarded", "bump") in _kinds(text)


def test_required_true_is_reported_even_when_forwarded() -> None:
    """A required `tag` breaks a from-head dispatch, which sends no tag."""
    text = COMPLIANT.replace(
        "      tag:\n        type: string\n        required: false",
        "      tag:\n        type: string\n        required: true",
    )
    assert ("required", "tag") in _kinds(text)


def test_yaml_boolean_on_key_is_handled() -> None:
    """PyYAML resolves a bare `on:` to True, which must not hide the inputs."""
    import yaml

    doc = yaml.safe_load(COMPLIANT)
    assert True in doc or "on" in doc
    assert audit_text("x", COMPLIANT).declared == ["bump", "from-head", "tag"]


def test_non_caller_is_not_audited() -> None:
    text = """
name: CI
on:
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
    report = audit_text("not-a-consumer", text)
    assert report.calls is None
    assert report.error is not None


def test_unparseable_yaml_reports_rather_than_raises() -> None:
    report = audit_text("broken", "name: [unclosed\n")
    assert report.error is not None
    assert not report.ok


def test_missing_ci_yml_reports(tmp_path) -> None:
    report = audit_local(tmp_path)
    assert not report.ok
    assert report.error is not None


@pytest.mark.parametrize("name", DISPATCH_INPUTS)
def test_every_contract_input_is_checked(name: str) -> None:
    """Adding to DISPATCH_INPUTS must extend the audit, not bypass it."""
    assert ("missing", name) in _kinds(
        """
name: CI
on:
  workflow_dispatch:
jobs:
  ci:
    uses: hyperi-io/hyperi-ci/.github/workflows/rust-ci.yml@main
    with:
      publish-target: both
"""
    )


def test_dispatch_cmd_rejects_an_undeclared_input() -> None:
    """An input sent but absent from the contract would 422 every consumer."""
    with pytest.raises(ValueError, match="DISPATCH_INPUTS"):
        _dispatch_cmd("ci.yml", {"not-a-real-input": "x"})


def test_dispatch_cmd_builds_the_gh_invocation() -> None:
    assert _dispatch_cmd("ci.yml", {"tag": "v1.2.3"}) == [
        "gh",
        "workflow",
        "run",
        "ci.yml",
        "-f",
        "tag=v1.2.3",
    ]
