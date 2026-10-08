# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/semver_checks.py
# Purpose:   Fail a library release whose public API broke without a major bump
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Check a published library crate for an unannounced breaking change (issue #186).

ORDERING IS THE WHOLE CONTRACT: this must run AFTER ``stamp-version``.
``cargo semver-checks`` takes the current version from Cargo.toml, and before
the stamp that is the last released version, so the tool compares a release
against itself and passes vacuously.

A binary-only crate and a crate not yet on crates.io are skipped, each with
its own log line, so a skip never reads as "ran and found nothing".
"""

import shutil
from pathlib import Path

from hyperi_ci.common import announce, error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode

_TOOL = "cargo-semver-checks"

# The only exit code that means the API broke; 101 is cargo's error and no verdict.
_BREAKING = 100

# Upstream's wording for an unpublished crate; a reworded message fails loudly.
_NO_BASELINE = "not found in registry"


def run(config: CIConfig, *, project_root: Path | None = None) -> int:
    """Check the crate's public API against the last published version.

    Must run AFTER `stamp-version`; see the module docstring.

    Args:
        config: Merged CI configuration.
        project_root: Crate root; defaults to the working directory.

    Returns:
        0 when the API is compatible, skipped, or the mode is not blocking.

    """
    mode = resolve_tool_mode("semver_checks", config, language="rust")
    if mode == "disabled":
        info(f"  {_TOOL}: disabled")
        return 0

    # A module-scope import would be circular with quality.py.
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
            # Annotated rather than logged: a check that quietly did not run is
            # the failure this module exists to catch.
            missing = (
                f"{_TOOL} is not installed on this runner, so the public API "
                f"was NOT checked. This release is unverified for breaking "
                f"changes."
            )
            announce(missing, "hyperi-ci semver-checks skipped")
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
        # Blocking fails here too: an unreached verdict compared nothing.
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
