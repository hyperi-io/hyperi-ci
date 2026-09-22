# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/semver_checks.py
# Purpose:   Fail a library release whose public API broke without a major bump
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Check a published library crate for an unannounced breaking change.

We publish Rust libraries to crates.io and nothing checked whether a release
broke its public API. A real case: a feature stopped enabling another feature,
the commit was typed ``fix:``, and semver-checks scores that as requiring a
major (issue #186).

ORDERING IS THE WHOLE CONTRACT, so read this before moving the call site.
``cargo semver-checks`` takes the CURRENT version from Cargo.toml and looks up
the previous published version on crates.io as its baseline. Our release flow
does not commit the version to Cargo.toml -- ``stamp-version`` writes it on the
runner. So:

* AFTER the stamp, the manifest holds the version about to ship, the baseline
  resolves to what crates.io already has, and the delta is the real one.
* BEFORE the stamp, the manifest is stale. The tool reads the last released
  version as CURRENT, finds the same version published, and correctly reports
  nothing between them. It passes, having compared a release against itself.

That vacuous pass is the failure this module exists to prevent, so a call site
before the stamp reintroduces the bug it was written to catch.

A binary-only crate has no public API to break and is SKIPPED with a distinct
line -- "no library target" must never read the same as "ran and found
nothing", which is the ambiguity the whole issue family is about.
"""

import shutil
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode

_TOOL = "cargo-semver-checks"


def run(config: CIConfig, *, project_root: Path | None = None) -> int:
    """Check the crate's public API against the last published version.

    Must run AFTER `stamp-version`; see the module docstring for why a call
    before it passes without comparing anything.

    Args:
        config: Merged CI configuration.
        project_root: Crate root; defaults to the working directory.

    Returns:
        0 when the API is compatible, skipped, or the mode is not blocking.

    """
    mode = resolve_tool_mode("semver_checks", config, "rust")
    if mode == "disabled":
        info(f"  {_TOOL}: disabled")
        return 0

    # Imported here rather than at module scope: quality.py owns the lib-target
    # question and importing it at the top would make the two modules circular.
    from hyperi_ci.languages.rust.quality import _has_lib_target

    root = project_root or Path.cwd()
    if not _has_lib_target(root):
        info(f"  {_TOOL}: no library target -- no public API to break")
        return 0

    if shutil.which("cargo-semver-checks") is None:
        # Same rule as every other Rust tool: a missing tool is an environment
        # gap locally and a coverage gap in CI, where it must be installed.
        if mode == "blocking" and is_ci():
            error(f"  {_TOOL}: not installed (required)")
            return 1
        warn(f"  {_TOOL}: not installed (skipping locally)")
        return 0

    result = run_cmd(
        ["cargo", "semver-checks", "check-release", "--color", "never"],
        cwd=root,
        capture=True,
        check=False,
    )
    if result.returncode == 0:
        success(f"  {_TOOL}: public API is compatible with the published version")
        return 0

    combined = (result.stdout or "") + (result.stderr or "")
    detail = combined.strip().splitlines()
    for line in detail[-20:]:
        info(f"    {line}")

    message = (
        "the public API changed in a way semver says needs a major bump. "
        "Either restore compatibility, or release it as a major -- which is a "
        "human decision, not a CI one."
    )
    if mode == "blocking":
        error(f"  {_TOOL}: {message}")
        return 1
    warn(f"  {_TOOL}: {message}")
    return 0
