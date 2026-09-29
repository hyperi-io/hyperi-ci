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
``HYPERCI_RELEASE_PREPARED`` set it takes every decision from the prepared
directory, so it never asks cargo a question that would run the repo's
toolchain file (issue #409).

cargo has no way to publish a ``.crate`` it did not package itself, so the
upload repackages the same stamped tree with ``--no-verify``. That builds
nothing and runs no build script. It runs from an empty directory with
``--manifest-path``, because cargo reads ``.cargo/config.toml`` and rustup reads
``rust-toolchain.toml`` from the working directory, and either can name a
program to run.
"""

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
from hyperi_ci.languages.rust.build import stamp_manifest

CRATE_FACT = "crate"


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


def _publishes_crate(config: CIConfig) -> bool:
    """Say whether this project ships a crate to a registry at all.

    Asks ``cargo metadata``, which runs the repo's toolchain file, so only the
    prepare half and a single-process run call it.
    """
    from hyperi_ci.languages.rust.build import _detect_binary_names

    if _detect_binary_names():
        info("Binary application — skipping crate registry publish")
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
            "No release version resolved — publishing with existing Cargo.toml version"
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
    if not _publishes_crate(config):
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
            error("cargo package failed — nothing is tagged or published")
            return result.returncode, {}
    return 0, {CRATE_FACT: True}


def _publish_crates_io(root: Path) -> int:
    """Publish the crate at ``root`` to crates.io without running its code.

    Requires CARGO_REGISTRY_TOKEN.

    Returns:
        Exit code (0 = success).

    """
    if not os.environ.get("CARGO_REGISTRY_TOKEN"):
        error("CARGO_REGISTRY_TOKEN not set — cannot publish to crates.io")
        return 1

    with tempfile.TemporaryDirectory(prefix="hyperi-ci-cargo-") as away:
        result = run_cmd(
            [
                "cargo",
                "publish",
                "--allow-dirty",
                "--no-verify",
                "--manifest-path",
                str(root / "Cargo.toml"),
            ],
            cwd=away,
            capture=True,
            check=False,
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
    job, and this only stamps the manifest (in Python) and publishes. Without
    it -- a hand-rolled workflow calling ``run release`` -- the checks run here
    first, in the same process as the token.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).

    """
    try:
        prepared = release_prepare.load()
    except release_prepare.PreparedError as exc:
        error(str(exc))
        return 1

    destinations = config.destination_for("cargo")
    if prepared is not None:
        if prepared.facts.get(CRATE_FACT) is not True:
            info("Prepare found no crate to publish")
            return 0
        if not destinations:
            info("No Rust publish destinations configured")
            return 0
        version = _read_version()
        if version and not _sync_cargo_toml_version(version):
            return 1
    else:
        if not _publishes_crate(config):
            return 0
        if _stamp_and_check(config) != 0:
            return 1

    info(f"Publishing Rust crate to: {', '.join(destinations)}")
    for dest in destinations:
        if dest == "crates-io":
            with group("Publish: crates.io"):
                rc = _publish_crates_io(Path.cwd())
                if rc != 0:
                    return rc
        else:
            error(f"Unknown Rust publish destination: {dest}")
            return 1

    return 0
