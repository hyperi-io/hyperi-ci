# Project:   HyperI CI
# File:      src/hyperi_ci/preflight.py
# Purpose:   Check publish credentials before the build, not after it
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Fail a publish run for a missing credential in seconds, not in an hour.

semantic-release runs a ``verifyConditions`` step for exactly this: prove the
credentials are there before doing any work. Our config is tagger-only and
implements no such step, so a missing ``CARGO_REGISTRY_TOKEN`` surfaced at the
publish stage -- after a Tier 2 PGO + BOLT Rust build, 35-45 minutes in.

Checked against the destinations the project actually publishes to, so a repo
that opted out of an artefact is never asked for its credential.

Severity follows what the publish handler does without the credential:

* **blocking** -- the handler hard-fails (crates.io, npm).
* **warn** -- the handler has a documented fallback (PyPI falls back to OIDC
  trusted publishing) or skips one destination of several (R2). A warn is
  still worth printing at plan time: an R2 skip is a partial publish that
  otherwise passes green.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.detect import detect_language


@dataclass(frozen=True)
class Requirement:
    """One destination's credentials, and what happens without them."""

    destination: str
    variables: tuple[str, ...]
    blocking: bool
    consequence: str


# Keyed by the destination identifier in `publish.destinations_oss`.
_REQUIREMENTS: dict[str, Requirement] = {
    "crates-io": Requirement(
        destination="crates.io",
        variables=("CARGO_REGISTRY_TOKEN",),
        blocking=True,
        consequence="cargo publish cannot authenticate",
    ),
    "npmjs": Requirement(
        destination="npm",
        variables=("NPM_TOKEN",),
        blocking=True,
        consequence="npm publish cannot authenticate",
    ),
    "pypi": Requirement(
        destination="PyPI",
        variables=("PYPI_TOKEN",),
        blocking=False,
        consequence="the upload falls back to OIDC trusted publishing",
    ),
    "r2-binaries": Requirement(
        destination="Cloudflare R2",
        variables=("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"),
        blocking=False,
        consequence="binaries reach GitHub Releases but not downloads.hyperi.io",
    ),
}

# The artefact key each language publishes under, in `destinations_oss`.
_LANGUAGE_ARTEFACT: dict[str, str] = {
    "python": "python",
    "rust": "cargo",
    "typescript": "npm",
    "javascript": "npm",
    "golang": "go",
}

# Checked for every language: any project may ship binaries.
_ALWAYS_CHECKED = ("binaries",)


def _publishes_a_crate(project_dir: Path | None) -> bool:
    """Report whether this project publishes a crate at all.

    ``languages.rust.publish.run`` returns early for a crate with ``[[bin]]``
    targets, whatever ``destinations_oss.cargo`` says -- its artefacts go to
    GitHub Releases and R2 instead. Asking a binary app for a
    ``CARGO_REGISTRY_TOKEN`` would block a release that never needed one.
    """
    from hyperi_ci.languages.rust.build import _detect_binary_names

    cwd = Path.cwd()
    try:
        if project_dir:
            os.chdir(project_dir)
        return not _detect_binary_names()
    finally:
        os.chdir(cwd)


def _artefact_keys(config: CIConfig, project_dir: Path | None) -> list[str]:
    """Collect the artefact keys this project's publish stage will touch."""
    language = detect_language(project_dir or Path.cwd())
    keys = list(_ALWAYS_CHECKED)
    artefact = _LANGUAGE_ARTEFACT.get(language or "")
    if artefact == "cargo" and not _publishes_a_crate(project_dir):
        artefact = None
    if artefact:
        keys.insert(0, artefact)
    return keys


def check_publish_credentials(
    config: CIConfig, *, project_dir: Path | None = None
) -> int:
    """Report missing credentials for the destinations this project publishes to.

    Args:
        config: Merged CI configuration.
        project_dir: Project root, for language detection. Defaults to cwd.

    Returns:
        0 when nothing blocking is missing, 1 otherwise.

    """
    missing_blocking: list[Requirement] = []
    checked = 0

    for key in _artefact_keys(config, project_dir):
        for destination in config.destination_for(key):
            requirement = _REQUIREMENTS.get(destination)
            if requirement is None:
                continue
            checked += 1
            absent = [
                name for name in requirement.variables if not os.environ.get(name)
            ]
            if not absent:
                success(f"  {requirement.destination}: credentials present")
                continue
            names = ", ".join(absent)
            if requirement.blocking:
                error(
                    f"  {requirement.destination}: {names} not set — "
                    f"{requirement.consequence}"
                )
                missing_blocking.append(requirement)
            else:
                warn(
                    f"  {requirement.destination}: {names} not set — "
                    f"{requirement.consequence}"
                )

    if not checked:
        info("No credential-bearing publish destinations configured")
        return 0

    if missing_blocking:
        names = ", ".join(name for req in missing_blocking for name in req.variables)
        error(
            f"Publish would fail on missing credentials: {names}. "
            "Set them as repository or organisation secrets, or opt out of the "
            "destination with `publish.destinations_oss.<artefact>: false`."
        )
        return 1

    success("Publish credentials verified")
    return 0


def run_preflight(config: CIConfig, *, project_dir: Path | None = None) -> int:
    """Run every pre-build check that a publish run depends on.

    Returns:
        0 when the run may proceed, 1 when it would fail later anyway.

    """
    if not is_ci():
        info("Preflight: not in CI — credentials are a CI concern, skipping")
        return 0
    info("Preflight: verifying publish credentials")
    return check_publish_credentials(config, project_dir=project_dir)
