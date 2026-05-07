# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/publish.py
# Purpose:   Rust publish handler (crates.io)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust publish handler — publishes crates to crates.io."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from hyperi_ci.common import error, group, info, success, warn
from hyperi_ci.config import CIConfig


def _read_version() -> str | None:
    """Read version from VERSION file (written by semantic-release).

    Returns:
        Version string (e.g. '1.2.3') or None if not found.

    """
    version_file = Path("VERSION")
    if not version_file.exists():
        return None
    version = version_file.read_text().strip()
    return version if version else None


def _sync_cargo_toml_version(version: str) -> bool:
    """Update version in Cargo.toml to match VERSION file.

    Args:
        version: Semver version string (e.g. '1.2.3').

    Returns:
        True if updated successfully, False on error.

    """
    cargo_toml = Path("Cargo.toml")
    if not cargo_toml.exists():
        error("Cargo.toml not found")
        return False

    content = cargo_toml.read_text()
    updated = re.sub(
        r'^version\s*=\s*"[^"]*"',
        f'version = "{version}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )

    if updated == content:
        warn("Could not find version field in Cargo.toml to update")
        return True

    cargo_toml.write_text(updated)
    info(f"  Updated Cargo.toml version to {version}")
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

    # Read version from VERSION file and sync to Cargo.toml
    version = _read_version()
    if version:
        info(f"Publishing version {version}")
        if not _sync_cargo_toml_version(version):
            return 1
    else:
        warn("No VERSION file found — publishing with existing Cargo.toml version")

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
