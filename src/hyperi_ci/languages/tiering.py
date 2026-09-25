# Project:   HyperI CI
# File:      src/hyperi_ci/languages/tiering.py
# Purpose:   Test tier (core | full) resolution and reporting
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Test tier: which part of a project's suite the test stage runs.

``core`` is the suite as the project selects it by default -- its own pytest
``addopts -m`` deselection, nextest's ``#[ignore]`` and ``default-filter``.
``full`` runs everything the runner can execute on top of that.

Set by ``test.tier``, ``HYPERCI_TEST_TIER`` or ``--tier``. Not to be confused
with ``test.use_tiers`` / ``test.tiers.*`` (the Python directory split) or
``test.rust.tier`` (the Rust unit / integration / e2e subset): those pick WHERE
tests live, this picks whether the deselected and ignored ones run.
"""

import re
from collections import deque
from enum import StrEnum

from hyperi_ci.common import announce, strip_ansi
from hyperi_ci.config import CIConfig

TEST_TIER_KEY = "test.tier"

# The key ``stage_test`` puts the resolved tier under in a handler's
# ``extra_env``, the channel it already uses for ``RUST_FEATURES``.
TEST_TIER_ENV = "TEST_TIER"


class SuiteTier(StrEnum):
    """The two test tiers."""

    CORE = "core"
    FULL = "full"


class InvalidTestTierError(ValueError):
    """``test.tier`` holds something other than ``core`` or ``full``."""


def resolve_test_tier(config: CIConfig) -> SuiteTier:
    """Return the configured test tier, refusing anything unrecognised.

    Args:
        config: Merged CI configuration.

    Returns:
        The tier, ``core`` when nothing sets one. An empty value counts as
        unset, so a workflow passing an empty output does not fail the stage.

    Raises:
        InvalidTestTierError: The value is not ``core`` or ``full``. A typo
            here must not quietly run the smaller suite.

    """
    raw = config.get(TEST_TIER_KEY, SuiteTier.CORE.value)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return SuiteTier.CORE
    if isinstance(raw, str):
        try:
            return SuiteTier(raw.strip().lower())
        except ValueError:
            pass
    allowed = ", ".join(tier.value for tier in SuiteTier)
    raise InvalidTestTierError(
        f"{TEST_TIER_KEY} is {raw!r}; expected one of {allowed}. Set it in "
        f".hyperi-ci.yaml, HYPERCI_TEST_TIER or --tier."
    )


def handler_tier(extra_env: dict[str, str] | None) -> SuiteTier:
    """Return the tier ``stage_test`` resolved, ``core`` when called without one.

    Args:
        extra_env: The handler's ``extra_env``.

    Returns:
        The tier the stage asked for.

    """
    return SuiteTier((extra_env or {}).get(TEST_TIER_ENV, SuiteTier.CORE.value))


class KeptLines:
    """An ``on_line`` sink that keeps the lines matching a pattern, bounded.

    ``stream_cmd`` returns only the tail of a run's output, and a summary can
    be spread through it (one libtest line per test binary), so a handler
    collects what it parses as the lines go by.
    """

    def __init__(self, pattern: re.Pattern[str], limit: int = 4096) -> None:
        """Keep up to ``limit`` of the most recent lines matching ``pattern``."""
        self._pattern = pattern
        self._lines: deque[str] = deque(maxlen=limit)

    def __call__(self, line: str) -> None:
        """Keep ``line``, colour stripped, when it matches."""
        clean = strip_ansi(line)
        if self._pattern.search(clean):
            self._lines.append(clean)

    def text(self) -> str:
        """Return the kept lines, newline-joined."""
        return "\n".join(self._lines)


def announce_tier(tier: SuiteTier, detail: str) -> None:
    """Report what a test run did not run, as a notice annotation in CI.

    Args:
        tier: The tier that ran.
        detail: The counts, e.g. ``"7892 passed, 17 skipped, 281 deselected"``.

    """
    announce(f"tier {tier}: {detail}", f"test tier {tier}", level="notice")


def warn_full_ran_core(reason: str) -> None:
    """Warn that a full-tier run ran the core command, so it cannot pass as full.

    Args:
        reason: Why nothing extra ran, e.g. ``"package.json has no test:full"``.

    """
    announce(
        f"tier full ran the same command as core: {reason}",
        "test tier full",
        level="warning",
    )
