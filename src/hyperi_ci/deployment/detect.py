# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/detect.py
# Purpose:   Tier auto-detection for the three-tier deployment-contract model
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Detect which producer tier a repository belongs to.

Used by Quality drift checks and the Generate stage to dispatch to the
right producer:

  Tier 1 (RUST)   — Cargo.toml depends on hyperi-rustlib; the binary
                    itself emits artefacts via `<app> generate-artefacts`.
  Tier 2 (PYTHON) — pyproject.toml depends on hyperi-pylib; the entry
                    point emits via `<app> generate-artefacts`.
  Tier 3 (OTHER)  — repo commits ``ci/deployment-contract.json``;
                    hyperi-ci's own templater emits.
  NONE            — no contract at all; container stage skips silently.

Detection is **cheap and string-based** — we don't fully parse manifests
because the answer only needs to choose which subprocess to invoke.
False-positive tier RUST that has no rustlib dep would fail at the
``generate-artefacts`` invocation with a clear error; not silently
producing wrong artefacts.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

__all__ = ["Tier", "detect_tier"]


class Tier(StrEnum):
    """Three-tier producer model — which one this repo uses."""

    RUST = "rust"
    PYTHON = "python"
    OTHER = "other"
    NONE = "none"


def detect_tier(repo_root: Path) -> Tier:
    """Detect the producer tier for a repository.

    Order of precedence:
      1. Cargo.toml + hyperi-rustlib in deps → :attr:`Tier.RUST`
      2. pyproject.toml + hyperi-pylib in deps → :attr:`Tier.PYTHON`
      3. ``ci/deployment-contract.json`` exists → :attr:`Tier.OTHER`
      4. Otherwise → :attr:`Tier.NONE`

    A repo may have multiple manifests (e.g. a Rust workspace with a
    Python subdir). The first match wins so the dispatch ordering is
    deterministic and matches the spec's documentation order.

    Args:
        repo_root: Directory containing the repo's manifests. Usually
            the working directory of a CI run.

    Returns:
        The detected :class:`Tier`.

    """
    cargo_toml = repo_root / "Cargo.toml"
    if cargo_toml.exists() and _depends_on(cargo_toml, "hyperi-rustlib"):
        return Tier.RUST

    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists() and _depends_on(pyproject, "hyperi-pylib"):
        return Tier.PYTHON

    if (repo_root / "ci" / "deployment-contract.json").is_file():
        return Tier.OTHER

    return Tier.NONE


def _depends_on(manifest: Path, package_name: str) -> bool:
    """Return True if a manifest's text contains the named dep.

    Substring match against the file contents — sufficient for tier
    detection because we only need a one-shot routing decision.
    Handles all the common forms (string, table, workspace inheritance):

        # Cargo.toml — single line
        hyperi-rustlib = "1.0"
        hyperi-rustlib = { version = "1.0", features = [...] }

        # Cargo.toml — workspace inheritance
        hyperi-rustlib.workspace = true

        # pyproject.toml — list
        dependencies = ["hyperi-pylib>=2.24"]
        dependencies = ["hyperi-pylib[metrics]>=2.24"]

    Doesn't try to parse TOML because:
      1. Avoids hauling `tomllib` in just for tier detection.
      2. Catches every form (workspace inheritance, extras, comments)
         that a stricter parse would have to handle case by case.
      3. False positives only if the package name appears in another
         context (e.g., a comment mentioning it) — acceptable, since
         the consequence is "we try to invoke generate-artefacts and
         the binary fails clearly" rather than silent miscategorisation.

    Args:
        manifest: Path to Cargo.toml or pyproject.toml.
        package_name: Dependency name to look for.

    Returns:
        True if the substring appears in the manifest's text.

    """
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return package_name in text
