# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/publish.py
# Purpose:   Rust publish handler (crates.io)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust publish handler — publishes crates to crates.io."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hyperi_ci.common import (
    error,
    group,
    info,
    resolve_release_version,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust.build import stamp_manifest


def _read_version() -> str | None:
    """Read the version being published (HYPERCI_VERSION-first).

    See common.resolve_release_version (issue #27 + zero-config).
    """
    return resolve_release_version()


def _sync_cargo_toml_version(version: str) -> bool:
    """Stamp the publish-job's Cargo.toml to the release version.

    The publish job's checkout is the committed (stale) tree, so Cargo.toml
    must be stamped before `cargo publish`. Delegates to the shared
    `stamp_manifest` — the SAME table-scoped stamper the build uses, so the
    two can't drift (the old unscoped regex here could clobber a dependency's
    `version =`).

    Returns False only if there's no Cargo.toml.
    """
    if not Path("Cargo.toml").exists():
        error("Cargo.toml not found")
        return False
    stamp_manifest(version, Path.cwd())
    return True


def _publish_crates_io() -> int:
    """Publish to crates.io.

    Requires CARGO_REGISTRY_TOKEN env var.

    Returns:
        Exit code (0 = success).

    """
    token = os.environ.get("CARGO_REGISTRY_TOKEN")
    if not token:
        error("CARGO_REGISTRY_TOKEN not set — cannot publish to crates.io")
        return 1

    result = subprocess.run(
        # --no-verify: CI build step already verified; publish runner lacks native
        # build tools (e.g. protoc) required by build scripts during cargo package.
        ["cargo", "publish", "--allow-dirty", "--no-verify"],
        env={**os.environ, "CARGO_REGISTRY_TOKEN": token},
        capture_output=True,
        text=True,
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
    """Run Rust publish stage.

    For library crates: reads VERSION file, syncs to Cargo.toml, publishes
    to crates.io.

    For binary apps: skips crate publish entirely. Binary artifacts are
    uploaded by the generic publish_binaries handler in dispatch.py.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).

    """
    from hyperi_ci.languages.rust.build import _detect_binary_names

    if _detect_binary_names():
        info("Binary application — skipping crate registry publish")
        info("Binary artifacts will be uploaded by the generic binary publisher")
        return 0

    destinations = config.destination_for("cargo")
    if not destinations:
        info("No Rust publish destinations configured")
        return 0

    # Resolve the release version (HYPERCI_VERSION-first) + stamp Cargo.toml
    version = _read_version()
    if version:
        info(f"Publishing version {version}")
        if not _sync_cargo_toml_version(version):
            return 1
    else:
        warn(
            "No release version resolved — publishing with existing Cargo.toml version"
        )

    info(f"Publishing Rust crate to: {', '.join(destinations)}")

    for dest in destinations:
        if dest == "crates-io":
            with group("Publish: crates.io"):
                rc = _publish_crates_io()
                if rc != 0:
                    return rc

        else:
            error(f"Unknown Rust publish destination: {dest}")
            return 1

    return 0
