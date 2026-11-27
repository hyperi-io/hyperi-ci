# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/detect.py
# Purpose:   Tier auto-detection for the three-tier deployment-contract model
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Detect which producer tier a repository belongs to.

  Tier 1 (RUST)   -- Cargo.toml depends on scalo (or legacy
                    hyperi-rustlib) AND the crate builds a binary; the
                    binary itself emits artefacts via
                    `<app> generate-artefacts`.
  Tier 2 (PYTHON) -- pyproject.toml depends on scalo AND declares a
                    `[project.scripts]` console script; that entry point
                    emits via `<app> generate-artefacts`.
  Tier 3 (OTHER)  -- repo commits ``ci/deployment-contract.json``.
  NONE            -- no contract at all.

Most repos resolve to NONE, which is not a failure.

Carrying the marker dep does not make a repo a producer: a library
consumer has the dep but nothing to invoke ``generate-artefacts`` on. Tier
1/2 therefore also need a real binary / console script (issue #76). Tier 3
needs no such check because the committed contract is the signal.

Detection is string-based, not a manifest parse.
"""

from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from hyperi_ci.deployment.manifest import (
    effective_dep_features,
    manifest_self_name,
    produces_rust_binary,
    python_entry_point,
    resolve_workspace_members,
)

__all__ = ["Tier", "TierDecision", "enables_bench_memory", "resolve_tier"]


class Tier(StrEnum):
    """Three-tier producer model -- which one this repo uses."""

    RUST = "rust"
    PYTHON = "python"
    OTHER = "other"
    NONE = "none"


class TierDecision(NamedTuple):
    """A tier plus the human-readable reason it was chosen."""

    tier: Tier
    reason: str


# Marker deps in match precedence. ``hyperi-rustlib`` is the archived Rust
# predecessor, kept so repos mid-migration still detect.
_RUST_DEPLOYMENT_DEPS: tuple[str, ...] = ("scalo", "hyperi-rustlib")
_PYTHON_DEPLOYMENT_DEPS: tuple[str, ...] = ("scalo",)

# The scalo-rs cargo feature that compiles in contract emission. Without it
# `generate-artefacts` exits 0 and writes no contract.
_DEPLOYMENT_FEATURE = "deployment"
# The scalo-rs feature that adds `<app> bench-memory`, which measures the
# contract's `memory` block.
_BENCH_MEMORY_FEATURE = "bench-memory"


def resolve_tier(repo_root: Path) -> TierDecision:
    """Detect the producer tier, with the reason it was chosen.

    Order of precedence:
      1. Cargo.toml + scalo (or legacy hyperi-rustlib) in deps, and the
         crate builds a binary -> :attr:`Tier.RUST`
      2. pyproject.toml + scalo in deps, and a ``[project.scripts]``
         console script is declared -> :attr:`Tier.PYTHON`
      3. ``ci/deployment-contract.json`` exists -> :attr:`Tier.OTHER`
      4. Otherwise -> :attr:`Tier.NONE`

    The first match wins. A repo that carries a marker dep but fails the
    producer check falls through to the Tier 3 check only, never to
    another manifest's signal: a Rust repo with no binary must not be
    dispatched to a Python tools subdir's entry point, which would emit
    the wrong artefacts silently.

    Args:
        repo_root: Directory containing the repo's manifests. Usually
            the working directory of a CI run.

    Returns:
        The detected :class:`TierDecision`.

    """
    rust_dep = _marker_dep(repo_root / "Cargo.toml", _RUST_DEPLOYMENT_DEPS)
    rust_reason = ""
    if rust_dep is not None:
        if not produces_rust_binary(repo_root):
            rust_reason = (
                f"depends on {rust_dep} but builds no binary "
                "(library consumer, not a deployment-artefact producer)"
            )
        elif not _enables_deployment_feature(repo_root, rust_dep):
            rust_reason = (
                f"depends on {rust_dep} without the '{_DEPLOYMENT_FEATURE}' "
                "feature, so its generate-artefacts emits no contract "
                "(not a deployment-artefact producer)"
            )
        else:
            return TierDecision(Tier.RUST, f"Cargo.toml depends on {rust_dep}")

    # A demoted Rust dep suppresses the Python check (see resolve_tier).
    python_dep = (
        None
        if rust_dep is not None
        else _marker_dep(repo_root / "pyproject.toml", _PYTHON_DEPLOYMENT_DEPS)
    )
    if python_dep is not None:
        if python_entry_point(repo_root) is not None:
            return TierDecision(Tier.PYTHON, f"pyproject.toml depends on {python_dep}")

    if (repo_root / "ci" / "deployment-contract.json").is_file():
        return TierDecision(Tier.OTHER, "ci/deployment-contract.json is committed")

    if rust_reason:
        return TierDecision(Tier.NONE, rust_reason)
    if python_dep is not None:
        return TierDecision(
            Tier.NONE,
            f"depends on {python_dep} but declares no [project.scripts] "
            "entry point (library consumer, not a deployment-artefact producer)",
        )
    return TierDecision(Tier.NONE, "no deployment contract present")


def _marker_dep(manifest: Path, candidates: tuple[str, ...]) -> str | None:
    """Return the first marker dep the manifest depends on, else None."""
    if not manifest.exists():
        return None
    return next((dep for dep in candidates if _depends_on(manifest, dep)), None)


def _enables_deployment_feature(repo_root: Path, dep_name: str) -> bool:
    """Return True when the marker crate's deployment feature is on.

    Without the feature scalo-rs compiles artefact emission out, so
    ``generate-artefacts`` exits 0 and writes no contract. Undetermined is
    permissive: an unparseable manifest dispatches and fails loudly rather
    than skipping a real producer.

    Rust-only: scalo-py's ``deployment`` extra is a pydantic pin, not a code gate.
    """
    return _scalo_feature(repo_root, dep_name, _DEPLOYMENT_FEATURE) is not False


def enables_bench_memory(repo_root: Path) -> bool:
    """Return True for a Rust producer that lists scalo's ``bench-memory`` feature.

    Only an explicit listing counts, not ``full``, and an unreadable manifest
    counts as off: a release fails when an app with the feature has no
    ``bench_pipeline``.
    """
    if resolve_tier(repo_root).tier is not Tier.RUST:
        return False
    dep = _marker_dep(repo_root / "Cargo.toml", _RUST_DEPLOYMENT_DEPS)
    if dep is None:
        return False
    return _scalo_feature(repo_root, dep, _BENCH_MEMORY_FEATURE) is True


def _scalo_feature(repo_root: Path, dep_name: str, feature: str) -> bool | None:
    """Return whether a crate turns ``feature`` on for ``dep_name``, None if none says.

    The repo root and every workspace member are checked, a ``workspace = true``
    entry taking its features from the root's ``[workspace.dependencies]`` via
    :func:`effective_dep_features`. One crate enabling it is enough, because
    cargo unifies a feature across the workspace build.
    """
    manifests = [repo_root / "Cargo.toml"]
    root_text = _read_manifest(repo_root / "Cargo.toml")
    if root_text:
        manifests.extend(
            member / "Cargo.toml"
            for member in resolve_workspace_members(repo_root, root_text)
        )

    determined = False
    for manifest in manifests:
        text = _read_manifest(manifest)
        if text is None:
            continue
        features = effective_dep_features(text, dep_name, root_text)
        if features is None:
            continue
        determined = True
        if feature in features:
            return True
    return False if determined else None


def _read_manifest(manifest: Path) -> str | None:
    """Read a manifest, tolerating absence."""
    if not manifest.is_file():
        return None
    try:
        return manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _depends_on(manifest: Path, package_name: str) -> bool:
    """Return True if the manifest text names the dep and is not that package.

    A substring match covers the string, table, workspace-inheritance and
    extras forms. A manifest whose own package name is ``package_name`` is the
    library itself, so it returns False: otherwise the library's own repo is
    dispatched as a consumer and fails with "no Rust binary found".
    :func:`resolve_tier` still demands a binary or console script, so a false
    positive here does not reach a producer.
    """
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if package_name not in text:
        return False
    return manifest_self_name(text) != package_name
