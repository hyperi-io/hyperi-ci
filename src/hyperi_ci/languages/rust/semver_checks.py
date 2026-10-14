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

A run that reaches no verdict fails in CI whatever the mode, because nothing
was compared. Locally it warns, and only ``disabled`` skips it everywhere.
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

_TAIL = 20

# Cargo prints the root cause first and the cascade after it, so on a big
# dependency tree the line that names the fault falls out of the tail.
_FIRST_ERRORS = 3


def _first_errors(lines: list[str]) -> list[str]:
    """Return the earliest distinct cargo ``error`` lines, in output order.

    Args:
        lines: The tool's combined stdout and stderr, split into lines.

    Returns:
        Up to ``_FIRST_ERRORS`` stripped lines starting ``error:`` or ``error[``.

    """
    found: list[str] = []
    for line in lines:
        text = line.strip()
        if not text.startswith(("error:", "error[")) or text in found:
            continue
        found.append(text)
        if len(found) == _FIRST_ERRORS:
            break
    return found


def run(config: CIConfig, *, project_root: Path | None = None) -> int:
    """Check the crate's public API against the last published version.

    Must run AFTER `stamp-version`; see the module docstring.

    Args:
        config: Merged CI configuration.
        project_root: Crate root; defaults to the working directory.

    Returns:
        0 when the API is compatible or skipped, or on a break in warn mode.
        1 on a break in blocking mode, and on no verdict in blocking mode or
        in CI.

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

    # On stable, not the project's rust-toolchain.toml: the tool needs a newer
    # rustc than an MSRV pin may give, and the API it compares does not change.
    result = run_cmd(
        ["cargo", "+stable", "semver-checks", "check-release", "--color", "never"],
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

    lines = combined.strip().splitlines()
    tail = lines[-_TAIL:]
    in_tail = {line.strip() for line in tail}
    first = _first_errors(lines)
    hidden = [line for line in first if line not in in_tail]
    if hidden:
        info("    first errors:")
        for line in hidden:
            info(f"      {line}")
    for line in tail:
        info(f"    {line}")

    if result.returncode == _BREAKING:
        message = (
            "the public API changed in a way semver says needs a major bump. "
            "Either restore compatibility, or release it as a major -- which "
            "is a human decision, not a CI one."
        )
        if mode == "blocking":
            error(f"  {_TOOL}: {message}")
            return 1
        warn(f"  {_TOOL}: {message}")
        return 0

    message = (
        f"the check could not reach a verdict (exit {result.returncode}). "
        f"A failed rustdoc build or a registry error lands here, and the "
        f"public API was NOT compared. Fix the error above and re-run."
    )
    if first:
        message += f" First error: {first[0]}"
    # In CI this fails even in warn mode: a green release here would ship an
    # API nobody compared (issue #678).
    if mode == "blocking" or is_ci():
        message += (
            " To release without the check, set quality.rust.semver_checks "
            "to {mode: disabled, reason: ...} in .hyperi-ci.yaml."
        )
        error(f"  {_TOOL}: {message}")
        return 1
    warn(f"  {_TOOL}: {message}")
    return 0
