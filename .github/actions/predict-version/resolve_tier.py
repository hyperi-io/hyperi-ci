#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/resolve_tier.py
# Purpose:   Write the run's test tier and the release opt-in to the step outputs
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Write ``test-tier`` and ``full-required-for-release`` to ``$GITHUB_OUTPUT``.

The decision lives in ``src/hyperi_ci/plan_tier.py``. The composite runs in the
caller's job, where hyperi-ci is not installed, so the implementation is loaded
out of the action's own checkout, the same by-path approach
``arm64_check.py`` takes.

Inputs arrive as environment variables, never interpolated into a script:
``TIER_EVENT``, ``TIER_WILL_RELEASE`` and ``TIER_REQUESTED``.

An unknown tier, from the input or the project's ``test.tier``, fails the step:
a typo must not quietly run core. Any other failure writes the tier the event
owes -- full for a schedule and for an opted-in release, else core -- and
warns.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs this composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[3] / "src" / "hyperi_ci"


def _write_outputs(tier: str, required: bool) -> None:
    lines = f"test-tier={tier}\nfull-required-for-release={'true' if required else 'false'}\n"
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(lines)
    else:
        sys.stdout.write(lines)


def _load_package() -> None:
    # hyperi_ci/__init__ reads installed package metadata, which is absent here.
    if "hyperi_ci" not in sys.modules:
        package = types.ModuleType("hyperi_ci")
        package.__path__ = [str(_PACKAGE)]
        sys.modules["hyperi_ci"] = package


def run() -> int:
    """Resolve the tier and the release opt-in, and write both outputs."""
    event = os.environ.get("TIER_EVENT", "")
    will_release = os.environ.get("TIER_WILL_RELEASE", "") == "true"
    required = False
    try:
        _load_package()
        from hyperi_ci.plan_tier import read_project_tier, resolve_tier

        workspace = Path(os.environ.get("GITHUB_WORKSPACE") or ".")
        project = read_project_tier(workspace)
        required = project.full_required
        tier, why = resolve_tier(
            event_name=event,
            will_release=will_release,
            requested=os.environ.get("TIER_REQUESTED", ""),
            project=project,
        )
    except ValueError as exc:
        print(f"::error title=test tier::{exc}")
        return 1
    except Exception as exc:
        # Restates plan_tier.owed_tier, because the failure may be its import.
        tier = "full" if event == "schedule" or (will_release and required) else "core"
        _write_outputs(tier, required)
        print(
            f"::warning title=test tier {tier}::tier resolution failed ({exc}) -- running the {event or 'event'} default"
        )
        return 0

    _write_outputs(tier, required)
    if project.unreadable:
        print(
            f"::warning title=test tier {tier}::{why}; {project.unreadable} could not be read, so its tier settings were ignored"
        )
    else:
        print(f"::notice title=test tier {tier}::{why}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
