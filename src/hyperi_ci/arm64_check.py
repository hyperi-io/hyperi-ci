# Project:   HyperI CI
# File:      src/hyperi_ci/arm64_check.py
# Purpose:   Whether a release-worthy merge to main owes an arm64 build
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Whether a release-worthy merge to main owes an arm64 build.

arm64 compiled only on a run that was already publishing, so an arm64-only
defect first executed during the release meant to ship it, and the fix for one
could not be exercised except by attempting another release (issue #249). A
BOLT refusal over Cortex-A53 veneers reached dfe-receiver exactly that way and
held two security fixes for over a week. The check builds below the release
tier, so it runs no PGO or BOLT: it catches compile and link defects, not that
one.

A release-worthy merge to main WILL ship, so compiling its arm64 leg crosses no
line in the gate doctrine: a merge that ships nothing still compiles nothing.
This module answers the project half of that decision -- does this repo build
aarch64 at all, and has it opted out with ``build.rust.arm64_on_main: false``.

The predict-version composite loads this by path on a runner where hyperi-ci is
not installed, so it is stdlib-only and imports nothing from the package but
:mod:`hyperi_ci.project_config`, which is stdlib-only for the same reason.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs the composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

from pathlib import Path

from hyperi_ci.project_config import read_project_config

#: The only arm64 target the build matrix carries a leg for.
AARCH64 = "aarch64-unknown-linux-gnu"

# YAML spells a false in several ways and a repo may quote it; each of these
# means the project has opted out.
_OFF = frozenset({"false", "no", "off", "0"})


def wants_arm64_check(root: Path) -> tuple[bool, str]:
    """Whether this project owes an arm64 build on a release-worthy merge.

    Args:
        root: The checkout root holding ``Cargo.toml`` and the project config.

    Returns:
        The decision, and the one line explaining it.

    """
    if not (root / "Cargo.toml").is_file():
        return False, "no Cargo.toml -- this project has no arm64 leg to run"

    config, name = read_project_config(root)
    if config is None:
        return False, f"{name} could not be read -- no arm64 check"

    build = config.get("build")
    rust = (build or {}).get("rust") if isinstance(build, dict) else None
    rust = rust if isinstance(rust, dict) else {}

    opt = rust.get("arm64_on_main")
    if opt is not None and str(opt).strip().lower() in _OFF:
        return False, "build.rust.arm64_on_main is off -- this project opted out"

    targets = rust.get("targets") or []
    if targets and AARCH64 not in targets:
        return False, f"build.rust.targets names no {AARCH64}"

    return True, f"release-worthy merge to main on a project that ships {AARCH64}"
