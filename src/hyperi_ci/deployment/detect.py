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

    **Self-match exclusion.** If the manifest declares its own package
    name as ``package_name`` (i.e. this manifest IS the library, not a
    consumer of it), returns False. Without this, the library's own
    repo gets misdispatched as a Tier 1/2 consumer and the
    deployment-artefact producer fails with "no Rust binary found" /
    equivalent. The check is generic — applies to any rustlib /
    pylib / future *lib and to consumer projects whose own name
    happens to share a prefix.

    Doesn't try to parse TOML beyond pulling the ``name`` field out of
    a recognised top-level section because:

      1. Avoids hauling `tomllib` in just for tier detection.
      2. Catches every form (workspace inheritance, extras, comments)
         that a stricter parse would have to handle case by case.
      3. False positives in the dep-match are bounded: the consequence
         is "we try to invoke generate-artefacts and the binary fails
         clearly" rather than silent miscategorisation.

    Args:
        manifest: Path to Cargo.toml or pyproject.toml.
        package_name: Dependency name to look for.

    Returns:
        True if the substring appears AND the manifest isn't itself
        the named package; False otherwise.

    """
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if package_name not in text:
        return False
    return _manifest_self_name(text) != package_name


# Top-level tables whose ``name`` field is the manifest's own package
# name. Listed in scan precedence — the first match wins, so a Cargo
# manifest's ``[package] name`` beats any later table (unlikely in
# Cargo, but pyproject.toml can legitimately have both ``[project]``
# and ``[tool.poetry]`` and we treat them equivalently).
_SELF_NAME_SECTIONS: frozenset[str] = frozenset(
    {"[package]", "[project]", "[tool.poetry]"}
)


def _manifest_self_name(text: str) -> str | None:
    """Extract the manifest's own package name, if declared.

    Scans for a ``name = "..."`` (or single-quoted) line inside one of
    the recognised self-name sections (:data:`_SELF_NAME_SECTIONS`).
    Returns the first match. No TOML parser — line-scoped, tolerant of
    extra whitespace around ``=``.

    Args:
        text: Full manifest text.

    Returns:
        The declared package name, or ``None`` if no recognised
        declaration is found.

    """
    current_section: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped
            continue
        if current_section not in _SELF_NAME_SECTIONS:
            continue
        # Cheap pre-filter: skip lines that don't even start with "name".
        if not stripped.startswith("name"):
            continue
        eq = stripped.find("=")
        if eq < 0:
            continue
        # Confirm the LHS is exactly "name" (avoids matching "name-foo").
        lhs = stripped[:eq].strip()
        if lhs != "name":
            continue
        rhs = stripped[eq + 1 :].strip()
        # Strip a trailing inline comment if present.
        if "#" in rhs:
            rhs = rhs[: rhs.index("#")].strip()
        if len(rhs) >= 2 and rhs[0] in {'"', "'"} and rhs[-1] == rhs[0]:
            return rhs[1:-1]
    return None
