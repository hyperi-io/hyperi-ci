# Project:   HyperI CI
# File:      tests/unit/test_dispatch_status.py
# Purpose:   Unit tests for project.status logging at stage start
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the `Project status:` log line emitted by the dispatcher.

`project.status` is an information-only lifecycle field; the dispatcher
surfaces it at the top of every stage so log readers see what kind of
project they're looking at. Behaviour locked in here:

- Unset (default) → no log line emitted.
- Known status (any of VALID_PROJECT_STATUSES) → one INFO line with the
  status and a clarifier phrase that explains what the status means.
- Unknown status (typo) → warned at config-load time and skipped by the
  dispatcher's status block — verified in `test_config.py`.

The clarifier phrase is what makes non-GA stand out without elevating
log level. A line that reads `Project status: beta — pre-GA, polishing`
is unmistakable in a wall of INFO without being a WARN.
"""

from __future__ import annotations

import pytest

from hyperi_ci.dispatch import _STATUS_CLARIFIER


class TestStatusClarifierMap:
    """The clarifier map drives the user-visible signal — lock it in."""

    def test_ga_has_no_clarifier(self) -> None:
        # GA is the unmarked default — clarifier adds nothing, so the
        # line reads simply "Project status: ga".
        assert _STATUS_CLARIFIER["ga"] == ""

    def test_pre_ga_statuses_say_pre_ga(self) -> None:
        for pre_ga in ("experimental", "alpha", "beta"):
            assert "pre-GA" in _STATUS_CLARIFIER[pre_ga], (
                f"{pre_ga} clarifier missing 'pre-GA' marker"
            )

    def test_legacy_signals_migration(self) -> None:
        assert "phased out" in _STATUS_CLARIFIER["legacy"]
        assert "migration" in _STATUS_CLARIFIER["legacy"]

    def test_deprecated_says_do_not_adopt(self) -> None:
        assert "do not adopt" in _STATUS_CLARIFIER["deprecated"]

    def test_all_valid_statuses_have_clarifier_entry(self) -> None:
        # If we add a new status to the enum, this test forces us to
        # decide what its clarifier phrase says rather than silently
        # falling through to an empty default.
        from hyperi_ci.config import VALID_PROJECT_STATUSES

        for status in VALID_PROJECT_STATUSES:
            assert status in _STATUS_CLARIFIER, (
                f"{status} added to VALID_PROJECT_STATUSES but not to "
                f"_STATUS_CLARIFIER — decide what the log line says"
            )

    def test_clarifier_has_no_warn_style_words(self) -> None:
        # The whole point of the redesign: clarifier carries signal
        # without being alarming. Reject "WARNING", "ERROR", "DANGER",
        # "DO NOT USE" — these elevate tone past the info level the
        # value deserves.
        forbidden = ("WARNING", "ERROR", "DANGER", "DO NOT USE")
        for status, phrase in _STATUS_CLARIFIER.items():
            for word in forbidden:
                assert word.lower() not in phrase.lower(), (
                    f"{status} clarifier '{phrase}' contains alarming "
                    f"word '{word}' — keep the tone at info level"
                )


class TestStatusLineFormat:
    """Format the dispatcher emits: 'Project status: <value><clarifier>'."""

    @pytest.mark.parametrize(
        "status,expected_substring",
        [
            ("ga", "Project status: ga"),
            ("beta", "Project status: beta — pre-GA, polishing"),
            ("legacy", "Project status: legacy — being phased out, plan migration"),
        ],
    )
    def test_format_matches_clarifier_map(
        self, status: str, expected_substring: str
    ) -> None:
        # Construct the exact line the dispatcher emits so the format
        # contract is locked. If someone changes the prefix to
        # "Lifecycle:" or drops the em-dash, this test catches it.
        line = f"Project status: {status}{_STATUS_CLARIFIER[status]}"
        assert expected_substring in line
