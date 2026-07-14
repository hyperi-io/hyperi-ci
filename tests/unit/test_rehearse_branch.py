# Project:   HyperI CI
# File:      tests/unit/test_rehearse_branch.py
# Purpose:   Tests for the branch-rehearsal ref swap
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/rehearse-branch.py pure helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "rehearse_branch",
    Path(__file__).resolve().parents[2] / "scripts" / "rehearse-branch.py",
)
assert _SPEC is not None and _SPEC.loader is not None
rehearse_branch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rehearse_branch)


class TestSwapRefs:
    def test_swaps_workflow_ref(self) -> None:
        text = "    uses: hyperi-io/hyperi-ci/.github/workflows/go-ci.yml@main\n"
        out, count = rehearse_branch.swap_refs(text, "fix/my-change")
        assert count == 1
        assert "hyperi-io/hyperi-ci/.github/workflows/go-ci.yml@fix/my-change" in out
        assert "@main" not in out

    def test_swaps_multiple_refs(self) -> None:
        text = (
            "uses: hyperi-io/hyperi-ci/.github/workflows/rust-ci.yml@main\n"
            "uses: hyperi-io/hyperi-ci/.github/workflows/_ghcr-prune.yml@main\n"
        )
        out, count = rehearse_branch.swap_refs(text, "fix/x")
        assert count == 2
        assert out.count("@fix/x") == 2

    def test_leaves_third_party_refs_alone(self) -> None:
        text = "uses: actions/checkout@main\nuses: dataaxiom/ghcr-cleanup-action@main\n"
        out, count = rehearse_branch.swap_refs(text, "fix/x")
        assert count == 0
        assert out == text

    def test_leaves_pinned_refs_alone(self) -> None:
        # A SHA-pinned or version-pinned hyperi-ci ref is deliberate — only
        # the floating @main refs are rehearsal targets.
        text = "uses: hyperi-io/hyperi-ci/.github/workflows/go-ci.yml@abc123\n"
        out, count = rehearse_branch.swap_refs(text, "fix/x")
        assert count == 0
        assert out == text

    def test_does_not_match_main_prefix_words(self) -> None:
        # @maintenance must not be treated as @main (word boundary).
        text = "uses: hyperi-io/hyperi-ci/.github/workflows/go-ci.yml@maintenance\n"
        out, count = rehearse_branch.swap_refs(text, "fix/x")
        assert count == 0
        assert out == text


class TestRehearseSlug:
    def test_slashes_collapse(self) -> None:
        assert rehearse_branch.rehearse_slug("fix/branch-rehearsal") == (
            "fix-branch-rehearsal"
        )

    def test_weird_chars_collapse_and_trim(self) -> None:
        assert rehearse_branch.rehearse_slug("feat/x y!(z)..") == "feat-x-y-z"

    def test_length_capped(self) -> None:
        assert len(rehearse_branch.rehearse_slug("x" * 200)) == 80
