# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/release.py
# Purpose:   Rust release handler (crates.io)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust release handler -- checks and packages a crate, then publishes it.

``prepare`` runs everything that executes the crate's own code:
cargo-semver-checks builds it, build scripts and all, and ``cargo package``
proves it packages before any tag is cut. ``run`` uploads. With
``HYPERCI_RELEASE_PREPARED`` set it requires prepare to have checked the crate,
and still works out for itself whether there is a library to publish, since a
forged ``prepared.json`` must not put a binary application on crates.io.

cargo has no way to publish a ``.crate`` it did not pack itself, so the upload
repackages its own checkout, Cargo.toml re-stamped, with ``--no-verify``. That
builds nothing and runs no build script. Every cargo call in the upload runs
from an empty directory with ``--manifest-path``, because cargo reads
``.cargo/config.toml`` and rustup reads ``rust-toolchain.toml`` from the working
directory, and either can name a program to run.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from hyperi_ci import release_prepare
from hyperi_ci.common import (
    error,
    group,
    info,
    resolve_release_version,
    run_cmd,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import semver_checks
from hyperi_ci.languages.rust.build import binary_targets, stamp_manifest

CRATE_FACT = "crate"


class CargoMetadataError(Exception):
    """``cargo metadata`` failed, so whether there is a crate is unknown."""


def _read_version() -> str | None:
    """Read the version being published (HYPERCI_VERSION-first).

    See common.resolve_release_version (issue #27 + zero-config).
    """
    return resolve_release_version()


def _sync_cargo_toml_version(version: str) -> bool:
    """Stamp the publish-job's Cargo.toml to the release version.

    The publish job's checkout is the committed (stale) tree, so Cargo.toml
    must be stamped before `cargo publish`. Delegates to the shared
    `stamp_manifest` -- the SAME table-scoped stamper the build uses, so the
    two can't drift (the old unscoped regex here could clobber a dependency's
    `version =`).

    Returns False only if there's no Cargo.toml.
    """
    if not Path("Cargo.toml").exists():
        error("Cargo.toml not found")
        return False
    stamp_manifest(version, Path.cwd())
    return True


def _cargo_away(root: Path, args: list[str], **kwargs: Any) -> Any:
    """Run ``cargo <args> --manifest-path <root>/Cargo.toml`` from an empty directory."""
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-cargo-") as away:
        return run_cmd(
            ["cargo", *args, "--manifest-path", str(root / "Cargo.toml")],
            cwd=away,
            check=False,
            **kwargs,
        )


def _binaries(root: Path) -> list[str]:
    """Return the crate's binary targets, failing rather than guessing.

    Raises:
        CargoMetadataError: cargo could not answer, so a library crate cannot
            be told from a binary application.

    """
    result = _cargo_away(
        root, ["metadata", "--no-deps", "--format-version=1"], capture=True
    )
    if result.returncode != 0:
        raise CargoMetadataError((result.stderr or "").strip()[-500:])
    try:
        return binary_targets(json.loads(result.stdout))
    except ValueError as exc:
        raise CargoMetadataError(f"unreadable cargo metadata: {exc}") from exc


def _publishes_crate(config: CIConfig, root: Path) -> bool:
    """Say whether this project ships a crate to a registry at all.

    Raises:
        CargoMetadataError: See :func:`_binaries`.

    """
    if _binaries(root):
        info("Binary application -- skipping crate registry publish")
        info("Binary artifacts will be uploaded by the generic binary publisher")
        return False
    if not config.destination_for("cargo"):
        info("No Rust publish destinations configured")
        return False
    return True


def _stamp_and_check(config: CIConfig) -> int:
    """Stamp Cargo.toml, then compare the public API with the last release.

    AFTER the stamp and BEFORE the publish, and both halves matter. Before the
    stamp the manifest is stale, so semver-checks compares the last release
    against itself and passes having checked nothing (issue #186).
    """
    version = _read_version()
    if version:
        info(f"Publishing version {version}")
        if not _sync_cargo_toml_version(version):
            return 1
    else:
        warn(
            "No release version resolved -- publishing with existing Cargo.toml version"
        )
    with group("Public API compatibility"):
        return semver_checks.run(config)


def prepare(config: CIConfig, out_dir: Path) -> tuple[int, dict[str, Any]]:
    """Run the crate checks that execute repo code, in the job with no secrets.

    Args:
        config: Merged CI configuration.
        out_dir: Prepared directory (unused: cargo cannot publish a prepared
            .crate, so nothing is carried but the decision).

    Returns:
        Exit code, and whether the upload has a crate to publish.

    """
    del out_dir
    try:
        publishes = _publishes_crate(config, Path.cwd())
    except CargoMetadataError as exc:
        error(f"cargo metadata failed, so the crate cannot be classified: {exc}")
        return 1, {}
    if not publishes:
        return 0, {CRATE_FACT: False}
    if _stamp_and_check(config) != 0:
        return 1, {}
    with group("Package crate"):
        result = run_cmd(
            # --no-verify matches the upload: the Build job compiled it already,
            # and this runner lacks the native build tools a build script needs.
            ["cargo", "package", "--allow-dirty", "--no-verify"],
            check=False,
        )
        if result.returncode != 0:
            error("cargo package failed -- nothing is tagged or published")
            return result.returncode, {}
    return 0, {CRATE_FACT: True}


def _publish_crates_io(root: Path) -> int:
    """Publish the crate at ``root`` to crates.io without running its code.

    Requires CARGO_REGISTRY_TOKEN.

    Returns:
        Exit code (0 = success).

    """
    if not os.environ.get("CARGO_REGISTRY_TOKEN"):
        error("CARGO_REGISTRY_TOKEN not set -- cannot publish to crates.io")
        return 1

    result = _cargo_away(
        root, ["publish", "--allow-dirty", "--no-verify"], capture=True
    )
    if result.returncode != 0:
        if "already exists" in result.stderr:
            warn("  Crate version already exists on crates.io (skipping)")
            return 0
        error("crates.io publish failed")
        if result.stderr:
            error(result.stderr)
        return result.returncode

    success("Published to crates.io")
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Upload a library crate to its registries.

    With ``HYPERCI_RELEASE_PREPARED`` set the checks already ran in the prepare
    job, and this only classifies the crate (from an empty directory), stamps
    the manifest in Python and publishes. Without it -- a hand-rolled workflow
    calling ``run release`` -- the checks run here first, in the same process
    as the token.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).

    """
    ok, prepared = release_prepare.load_or_report("run release")
    if not ok:
        return 1

    root = Path.cwd()
    try:
        publishes = _publishes_crate(config, root)
    except CargoMetadataError as exc:
        error(f"cargo metadata failed, so the crate cannot be classified: {exc}")
        return 1
    if not publishes:
        return 0

    if prepared is not None:
        if prepared.facts.get(CRATE_FACT) is not True:
            error(
                "This is a library crate with a registry destination, but the "
                "prepare job did not check and package it -- refusing to publish"
            )
            return 1
        version = _read_version()
        if version and not _sync_cargo_toml_version(version):
            return 1
    elif _stamp_and_check(config) != 0:
        return 1

    destinations = config.destination_for("cargo")
    info(f"Publishing Rust crate to: {', '.join(destinations)}")
    for dest in destinations:
        if dest == "crates-io":
            with group("Publish: crates.io"):
                rc = _publish_crates_io(root)
                if rc != 0:
                    return rc
        else:
            error(f"Unknown Rust publish destination: {dest}")
            return 1

    return 0
