#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/unreleased.py
# Purpose:   Report unreleased releasable work, as a warning once it is old
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Print the annotation for a validate-only run that leaves a release waiting.

A push to main with no release trailer validates and publishes nothing, which
is correct -- and indistinguishable from a release run at the rollup. scalo-rs
sat 13 releasable commits and 26 days behind crates.io, one of them a security
floor bump, and every run said SUCCESS.

Stdout carries the whole workflow command, ``::warning::<text>`` once the
oldest unreleased commit is older than ``release.unreleased_warn_days``, else
``::notice::<text>``, and is EMPTY when there is nothing waiting. Stderr always
says which answer this was and where the threshold came from, so a quiet run
still records why it was quiet.

The threshold is read from the project's config file, else the shipped
``defaults.yaml``, so this runs on the composite's config-reader Python. With
no readable threshold it warns, as it did before the age gate existed.

Loaded the same way as ``release_worthy.py``: the composite runs in the
caller's job where hyperi-ci is not installed, so ``hyperi_ci`` is registered
as a package pointing at ``src/hyperi_ci`` and the submodule imported by path.
``hyperi_ci/__init__`` reads installed package metadata and would raise here.

Prints nothing and exits 0 on ANY failure. This decides only whether an
annotation appears, so a crash must never take the release gate down with it.
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


def _warn_days(root: Path) -> float | None:
    """Return ``release.unreleased_warn_days``, or None when it cannot be read."""
    from hyperi_ci.commit_range import UNRELEASED_WARN_DAYS_KEY
    from hyperi_ci.project_config import setting

    value, source = setting(root, UNRELEASED_WARN_DAYS_KEY)
    if value is None:
        print(f"{UNRELEASED_WARN_DAYS_KEY}: {source}", file=sys.stderr)
        return None
    try:
        days = float(str(value))
    except ValueError:
        days = -1.0
    if days < 0:
        print(
            f"::warning title={UNRELEASED_WARN_DAYS_KEY}::{source} sets it to "
            f"{value!r}, which is not a number of days -- warning on any unreleased work"
        )
        return None
    print(f"{UNRELEASED_WARN_DAYS_KEY}={value} from {source}", file=sys.stderr)
    return days


def main() -> int:
    package = types.ModuleType("hyperi_ci")
    package.__path__ = [str(_PACKAGE)]
    sys.modules["hyperi_ci"] = package

    from hyperi_ci.commit_range import unreleased_annotation

    root = Path(os.environ.get("GITHUB_WORKSPACE") or ".")
    level, message = unreleased_annotation(warn_days=_warn_days(root))
    if level:
        print(f"unreleased work: {level}", file=sys.stderr)
        print(f"::{level}::{message}")
    else:
        print(message, file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"unreleased-work check failed: {exc}", file=sys.stderr)
        sys.exit(0)
