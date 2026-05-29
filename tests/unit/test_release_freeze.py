# Project:   HyperI CI
# File:      tests/unit/test_release_freeze.py
# Purpose:   Tests for internal-ref freezing (#31 Phase 2b)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""freeze-internals: rewrite hyperi-ci's own `@main` sibling refs → `@vX` so a
released `<lang>-ci.yml@vX` carries a frozen transitive graph (#31). External
action pins (setup-uv, checkout, rust-toolchain) are left untouched."""

from __future__ import annotations

from hyperi_ci.release.freeze import count_floating, freeze_text


class TestFreezeText:
    def test_freezes_composite_ref(self) -> None:
        line = "uses: hyperi-io/hyperi-ci/.github/actions/predict-version@main"
        out = freeze_text(line, "2.5.0")
        assert "predict-version@v2.5.0" in out
        assert "@main" not in out

    def test_freezes_reusable_workflow_ref(self) -> None:
        line = "uses: hyperi-io/hyperi-ci/.github/workflows/_release-tail.yml@main"
        out = freeze_text(line, "2.5.0")
        assert "_release-tail.yml@v2.5.0" in out

    def test_leaves_external_pins_untouched(self) -> None:
        text = (
            "uses: astral-sh/setup-uv@08807647 # v8.1.0\n"
            "uses: dtolnay/rust-toolchain@3c5f7ea # master\n"
            "uses: actions/checkout@de0fac2 # v6.0.2\n"
        )
        assert freeze_text(text, "2.5.0") == text

    def test_leaves_non_main_internal_alone(self) -> None:
        # already pinned to a tag → not @main → untouched
        line = "uses: hyperi-io/hyperi-ci/.github/actions/predict-version@v2.4.0"
        assert freeze_text(line, "2.5.0") == line

    def test_multiple_refs(self) -> None:
        text = (
            "      - uses: hyperi-io/hyperi-ci/.github/actions/setup-runtime@main\n"
            "      - uses: hyperi-io/hyperi-ci/.github/workflows/_release-tail.yml@main\n"
        )
        out = freeze_text(text, "3.0.1")
        assert "setup-runtime@v3.0.1" in out
        assert "_release-tail.yml@v3.0.1" in out
        assert "@main" not in out


class TestCountFloating:
    def test_counts_internal_main_refs(self) -> None:
        text = (
            "uses: hyperi-io/hyperi-ci/.github/actions/predict-version@main\n"
            "uses: hyperi-io/hyperi-ci/.github/workflows/_release-tail.yml@main\n"
            "uses: astral-sh/setup-uv@08807647 # v8.1.0\n"
        )
        assert count_floating(text) == 2

    def test_zero_after_freeze(self) -> None:
        text = "uses: hyperi-io/hyperi-ci/.github/actions/predict-version@main\n"
        assert count_floating(freeze_text(text, "2.5.0")) == 0
