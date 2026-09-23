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
nothing", which is the ambiguity the whole issue family is about. A crate not
yet on crates.io is skipped for the same reason: there is no baseline, so there
is nothing it could have broken.

Only exit 100 is a verdict. 101 is cargo's error code and says the comparison
never happened, so it is reported as an unreached verdict rather than a break.
"""

import shutil
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode

_TOOL = "cargo-semver-checks"

# The only exit code that means the API broke. 101 is cargo's error code and
# carries no verdict -- an unpublished crate, a failed rustdoc build and a
# registry error all land there.
_BREAKING = 100

# Upstream's wording when the crate has never been published. A reworded
# message falls through to "could not check", which fails loudly rather than
# passing.
_NO_BASELINE = "not found in registry"


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
        if mode == "blocking" and is_ci():
            error(f"  {_TOOL}: not installed (required)")
            return 1
        if is_ci():
            # Nothing installs it on a runner yet, so this is where every
            # release currently lands. Annotated rather than logged, because a
            # check that quietly did not run is the failure it exists to catch.
            missing = (
                f"{_TOOL} is not installed on this runner, so the public API "
                f"was NOT checked. This release is unverified for breaking "
                f"changes."
            )
            warn(f"  {missing}")
            print(f"::warning title=hyperi-ci semver-checks skipped::{missing}")
            return 0
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

    # A crate with no published baseline has no API to break. Skipped rather
    # than failed, and named, so a first release does not read as a violation.
    if _NO_BASELINE in combined:
        info(f"  {_TOOL}: not published yet -- no baseline to compare against")
        return 0

    for line in combined.strip().splitlines()[-20:]:
        info(f"    {line}")

    if result.returncode == _BREAKING:
        message = (
            "the public API changed in a way semver says needs a major bump. "
            "Either restore compatibility, or release it as a major -- which "
            "is a human decision, not a CI one."
        )
    else:
        # Blocking fails here too: publishing on an unreached verdict is the
        # "returned 0 having compared nothing" failure this module exists for.
        message = (
            f"the check could not reach a verdict (exit {result.returncode}). "
            f"A failed rustdoc build or a registry error lands here, and the "
            f"public API was NOT compared. Fix the error above and re-run; do "
            f"not read this as a clean result."
        )
    if mode == "blocking":
        error(f"  {_TOOL}: {message}")
        return 1
    warn(f"  {_TOOL}: {message}")
    return 0
