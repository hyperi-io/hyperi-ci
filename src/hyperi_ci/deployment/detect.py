# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/detect.py
# Purpose:   Tier auto-detection for the three-tier deployment-contract model
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Detect which producer tier a repository belongs to.

Used by Quality drift checks and the Generate stage to dispatch to the
right producer:

  Tier 1 (RUST)   — Cargo.toml depends on scalo (or legacy
                    hyperi-rustlib) AND the crate builds a binary; the
                    binary itself emits artefacts via
                    `<app> generate-artefacts`.
  Tier 2 (PYTHON) — pyproject.toml depends on scalo AND declares a
                    `[project.scripts]` console script; that entry point
                    emits via `<app> generate-artefacts`.
  Tier 3 (OTHER)  — repo commits ``ci/deployment-contract.json``;
                    hyperi-ci's own templater emits.
  NONE            — no contract at all; generate + container stages skip
                    silently.

Most repos are none of these and resolve to NONE — that is the normal
case, not a failure.

**Carrying the marker dep is not the same as being a producer.** A
library consumer — a VPN container that uses scalo for
logging/config/secrets and ships its own Dockerfile, say — has the dep
but nothing to invoke ``generate-artefacts`` on. Tier 1/2 detection
therefore needs a POSITIVE producer signal (a real binary / console
script) on top of the dep, otherwise the Build job dies on a repo that
was never a ServiceApp (issue #76). Tier 3 needs no such check: the
committed contract IS the positive signal.

Where auto-detection still gets it wrong in either direction, the
``deployment.producer`` cascade key is the override — see
:func:`hyperi_ci.deployment.stage.run`.

Detection is **cheap and string-based** — we don't fully parse manifests
because the answer only needs to choose which subprocess to invoke.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from hyperi_ci.deployment.manifest import (
    dep_features,
    manifest_self_name,
    produces_rust_binary,
    python_entry_point,
    resolve_workspace_members,
)

__all__ = ["Tier", "TierDecision", "detect_tier", "resolve_tier"]


class Tier(StrEnum):
    """Three-tier producer model — which one this repo uses."""

    RUST = "rust"
    PYTHON = "python"
    OTHER = "other"
    NONE = "none"


class TierDecision(NamedTuple):
    """A tier plus the human-readable reason it was chosen.

    The reason is what the Generate stage logs, so a repo that gets
    skipped says WHY it was skipped rather than going quiet.

    ``demoted`` marks the one case worth a nudge: a marker dep IS
    present but the producer signal isn't, so the repo reads as a
    library consumer. If that call is wrong, ``deployment.producer:
    true`` is the override — but only this case should suggest it.
    """

    tier: Tier
    reason: str
    demoted: bool = False


# Marker deps, in match precedence. ``scalo`` is the current lib name
# on BOTH sides (the crate scalo-rs on crates.io, and the scalo package
# on PyPI); ``hyperi-rustlib`` is the deprecated Rust predecessor, kept
# so consumer repos mid-migration still detect. The deprecated Python
# predecessor has no active consumers left, so it is not detected. No
# ambiguity from the shared ``scalo`` name: Tier 1 only reads
# Cargo.toml, Tier 2 only reads pyproject.toml.
_RUST_DEPLOYMENT_DEPS: tuple[str, ...] = ("scalo", "hyperi-rustlib")
_PYTHON_DEPLOYMENT_DEPS: tuple[str, ...] = ("scalo",)

# The scalo-rs cargo feature that compiles in contract emission. Without
# it `generate-artefacts` runs, exits 0, and writes no contract.
_DEPLOYMENT_FEATURE = "deployment"


def detect_tier(repo_root: Path) -> Tier:
    """Detect the producer tier for a repository.

    Thin wrapper over :func:`resolve_tier` for callers that only want
    the answer, not the reasoning.

    Args:
        repo_root: Directory containing the repo's manifests. Usually
            the working directory of a CI run.

    Returns:
        The detected :class:`Tier`.

    """
    return resolve_tier(repo_root).tier


def resolve_tier(repo_root: Path, *, require_producer: bool = True) -> TierDecision:
    """Detect the producer tier, with the reason it was chosen.

    Order of precedence:
      1. Cargo.toml + scalo (or legacy hyperi-rustlib) in deps, and the
         crate builds a binary → :attr:`Tier.RUST`
      2. pyproject.toml + scalo in deps, and a ``[project.scripts]``
         console script is declared → :attr:`Tier.PYTHON`
      3. ``ci/deployment-contract.json`` exists → :attr:`Tier.OTHER`
      4. Otherwise → :attr:`Tier.NONE`

    A repo may have multiple manifests (e.g. a Rust workspace with a
    Python subdir). The first match wins so the dispatch ordering is
    deterministic and matches the spec's documentation order.

    A repo that carries a marker dep but fails the producer check falls
    through to the Tier 3 check only. A committed contract is an
    explicit declaration, so a scalo library consumer that commits one
    is a legitimate Tier 3 repo. Another MANIFEST's implicit signal is
    not: a Rust repo whose crate builds no binary must not get
    dispatched to a Python tools subdir's entry point, which would emit
    the wrong artefacts silently. Skipping is recoverable; producing
    the wrong Dockerfile is not.

    Args:
        repo_root: Directory containing the repo's manifests. Usually
            the working directory of a CI run.
        require_producer: When False, the marker dep alone selects the
            tier. Set by ``deployment.producer: true`` for a genuine
            producer whose shape auto-detection can't see.

    Returns:
        The detected :class:`TierDecision`.

    """
    rust_dep = _marker_dep(repo_root / "Cargo.toml", _RUST_DEPLOYMENT_DEPS)
    rust_reason = ""
    if rust_dep is not None:
        if not require_producer:
            return TierDecision(
                Tier.RUST, f"Cargo.toml depends on {rust_dep} (producer forced)"
            )
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

    # A demoted Rust dep suppresses the Python check — see the
    # fall-through note above.
    python_dep = (
        None
        if rust_dep is not None
        else _marker_dep(repo_root / "pyproject.toml", _PYTHON_DEPLOYMENT_DEPS)
    )
    if python_dep is not None:
        if not require_producer:
            return TierDecision(
                Tier.PYTHON, f"pyproject.toml depends on {python_dep} (producer forced)"
            )
        if python_entry_point(repo_root) is not None:
            return TierDecision(Tier.PYTHON, f"pyproject.toml depends on {python_dep}")

    if (repo_root / "ci" / "deployment-contract.json").is_file():
        return TierDecision(Tier.OTHER, "ci/deployment-contract.json is committed")

    # Nothing matched. When a marker dep WAS present, the repo is a
    # library consumer rather than a producer — say so, because
    # "depends on scalo but the build skipped generate" is otherwise a
    # confusing pair of facts.
    if rust_reason:
        return TierDecision(Tier.NONE, rust_reason, demoted=True)
    if python_dep is not None:
        return TierDecision(
            Tier.NONE,
            f"depends on {python_dep} but declares no [project.scripts] "
            "entry point (library consumer, not a deployment-artefact producer)",
            demoted=True,
        )
    return TierDecision(Tier.NONE, "no deployment contract present")


def _marker_dep(manifest: Path, candidates: tuple[str, ...]) -> str | None:
    """Return the first marker dep the manifest depends on, else None."""
    if not manifest.exists():
        return None
    return next((dep for dep in candidates if _depends_on(manifest, dep)), None)


def _enables_deployment_feature(repo_root: Path, dep_name: str) -> bool:
    """Return True when the marker crate's deployment feature is on.

    In scalo-rs, ``deployment`` is a cargo feature, so the artefact
    emission inside ``generate-artefacts`` is ``#[cfg]``-compiled out
    when it's off. The subcommand still EXISTS and still exits 0 — it
    just writes no Dockerfile.runtime or container-manifest.json. The
    container stage then fails much later with "no deployment artefacts
    found", pointing at the wrong cause. Detecting it here turns that
    into a clean skip.

    Checks the repo root, then any workspace member (a member declaring
    ``scalo.workspace = true`` inherits the root's feature list, which
    :func:`dep_features` reports as unknown at the member).

    Unknown stays PERMISSIVE — an unparseable manifest dispatches and
    fails loudly rather than silently skipping a real producer.

    Note this is Rust-only. scalo-py's ``deployment`` extra is just a
    pydantic pin, not a code gate: dfe-engine emits contracts without
    declaring it, so the same test on Tier 2 would demote a real
    producer.
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
        features = dep_features(text, dep_name)
        if features is None:
            continue
        determined = True
        if _DEPLOYMENT_FEATURE in features:
            return True
    # Nothing anywhere told us what the features are -> assume producer.
    return not determined


def _read_manifest(manifest: Path) -> str | None:
    """Read a manifest, tolerating absence."""
    if not manifest.is_file():
        return None
    try:
        return manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _depends_on(manifest: Path, package_name: str) -> bool:
    """Return True if a manifest's text contains the named dep.

    Substring match against the file contents — sufficient for tier
    detection because we only need a one-shot routing decision.
    Handles all the common forms (string, table, workspace inheritance):

        # Cargo.toml — single line
        scalo = "2.0"
        scalo = { version = "2.0", features = [...] }

        # Cargo.toml — workspace inheritance
        scalo.workspace = true

        # pyproject.toml — list
        dependencies = ["scalo>=2.28"]
        dependencies = ["scalo[metrics]>=2.28"]

    **Self-match exclusion.** If the manifest declares its own package
    name as ``package_name`` (i.e. this manifest IS the library, not a
    consumer of it), returns False. Without this, the library's own
    repo gets misdispatched as a Tier 1/2 consumer and the
    deployment-artefact producer fails with "no Rust binary found" /
    equivalent. The check is generic — applies to any marker dep
    (scalo, the legacy hyperi-rustlib, future libs) and to consumer
    projects whose own name happens to share a prefix (scalo's own
    repo, for one).

    Doesn't try to parse TOML beyond pulling the ``name`` field out of
    a recognised top-level section because:

      1. Avoids hauling `tomllib` in just for tier detection.
      2. Catches every form (workspace inheritance, extras, comments)
         that a stricter parse would have to handle case by case.
      3. A false positive here no longer reaches a producer on its own
         — :func:`resolve_tier` still demands the binary / console
         script before it dispatches.

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
    return manifest_self_name(text) != package_name
