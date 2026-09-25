# Project:   HyperI CI
# File:      src/hyperi_ci/plan_tier.py
# Purpose:   Which test tier a CI run owes, decided in the plan job
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Which test tier a CI run owes, and whether a release must have run full.

Two tiers. ``core`` is what a PR and a push run. ``full`` adds every test the
project deselects or ignores by default. It runs on a scheduled run, on a run
given the ``test-tier: full`` workflow input, in a project whose own
``test.tier`` is full, and on a release once the project sets
``test.full.required_for_release``. Until then a release runs core, as it
always has, and the Gate says so. Nothing lowers a tier: ``core`` from the
input or the project is the absence of a request, not a request for less.

The plan job resolves this once. The Test job passes ``--tier full`` only on a
full run, and the Gate names the tier, so it is never re-derived in YAML.

The predict-version composite loads this by path on a runner where hyperi-ci is
not installed, so it is stdlib-only and imports nothing heavier than
:mod:`hyperi_ci.project_config`, which is stdlib-only for the same reason.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs the composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from hyperi_ci.project_config import read_project_config

CORE = "core"
FULL = "full"
TIERS = (CORE, FULL)

#: The project's own tier. Plan reads it as a floor: full here is never lowered.
TIER_KEY = "test.tier"

#: The opt-in that makes a release run full.
REQUIRED_FOR_RELEASE_KEY = "test.full.required_for_release"

# YAML spells a true in several ways and a repo may quote it.
_ON = frozenset({"true", "yes", "on", "1"})


class ProjectTier(NamedTuple):
    """What the project config says about tiers.

    Attributes:
        tier: The project's ``test.tier``, core when unset.
        full_required: Whether ``test.full.required_for_release`` is on.
        unreadable: The config file name when it exists and could not be
            read, else empty. Both settings then read as unset.

    """

    tier: str
    full_required: bool
    unreadable: str


def _lookup(config: dict, dotted: str) -> object:
    value: object = config
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def read_project_tier(root: Path) -> ProjectTier:
    """Read ``test.tier`` and ``test.full.required_for_release``.

    Every config spelling :func:`hyperi_ci.config.load_config` accepts is
    read, first found wins. The packaged defaults (core, off) are restated
    here because the plan job has no hyperi-ci install to read them from.

    Args:
        root: The checkout root.

    Returns:
        The two settings.

    Raises:
        ValueError: If ``test.tier`` names neither tier.

    """
    config, name = read_project_config(root)
    if config is None:
        return ProjectTier(CORE, False, name)

    raw_tier = _lookup(config, TIER_KEY)
    tier = str(raw_tier).strip().lower() if raw_tier is not None else CORE
    if tier not in TIERS:
        raise ValueError(
            f"{TIER_KEY} in {name} must be one of {', '.join(TIERS)}, not {raw_tier!r}"
        )

    required = _lookup(config, REQUIRED_FOR_RELEASE_KEY)
    enabled = required is True or (
        isinstance(required, str) and required.strip().lower() in _ON
    )
    return ProjectTier(tier, enabled, "")


def owed_tier(*, event_name: str, will_release: bool, full_required: bool) -> str:
    """Return the tier an event owes before any input or setting raises it.

    Args:
        event_name: ``github.event_name`` of the run.
        will_release: Whether the plan decided this run tags and publishes.
        full_required: Whether the project sets
            ``test.full.required_for_release``.

    Returns:
        full for a schedule and for an opted-in release, else core.

    """
    if event_name == "schedule" or (will_release and full_required):
        return FULL
    return CORE


def resolve_tier(
    *,
    event_name: str,
    will_release: bool,
    requested: str,
    project: ProjectTier,
) -> tuple[str, str]:
    """Decide the tier this run's tests execute at.

    The event's owed tier is the floor. The ``test-tier`` input and the
    project's ``test.tier`` can each raise it to full; neither lowers it.

    Args:
        event_name: ``github.event_name`` of the run.
        will_release: Whether the plan decided this run tags and publishes.
        requested: The ``test-tier`` workflow input, empty when not given.
        project: What the project config says.

    Returns:
        The tier, and the one line explaining it.

    Raises:
        ValueError: If ``requested`` names neither tier. A typo must not
            quietly run core on a run someone asked to run full.

    """
    wanted = requested.strip().lower() or CORE
    if wanted not in TIERS:
        raise ValueError(
            f"test-tier must be one of {', '.join(TIERS)}, not {requested!r}"
        )
    owed = owed_tier(
        event_name=event_name,
        will_release=will_release,
        full_required=project.full_required,
    )
    if owed == FULL and will_release:
        return FULL, f"this run releases and {REQUIRED_FOR_RELEASE_KEY} is on"
    if owed == FULL:
        return FULL, "scheduled run"
    if wanted == FULL:
        return FULL, "the test-tier input asked for full"
    if project.tier == FULL:
        return FULL, f"the project sets {TIER_KEY}: full"
    if will_release:
        return CORE, f"this run releases and {REQUIRED_FOR_RELEASE_KEY} is off"
    return CORE, f"{event_name or 'unknown'} run"
