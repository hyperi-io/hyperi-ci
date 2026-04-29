# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/publish.py
# Purpose:   Rust publish handler (crates.io + JFrog Cargo registry)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust publish handler.

Publishes Rust crates to crates.io (OSS) and/or JFrog Artifactory (internal)
depending on the publish target configuration.

Follows patterns from the old CI (ci/scripts/languages/rust/publish.sh) but
implemented in Python per the NO BASH principle.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from hyperi_ci.common import error, group, info, mask, success, warn
from hyperi_ci.config import CIConfig, load_org_config


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


def _configure_jfrog_registry(registry_name: str, index_url: str, token: str) -> None:
    """Write Cargo credentials and config for JFrog registry.

    Follows the old CI pattern: write credentials.toml and config.toml
    under CARGO_HOME so cargo can authenticate with the registry.

    Args:
        registry_name: Registry name (e.g. 'hyperi').
        index_url: Sparse index URL for the registry.
        token: Bearer token for authentication.

    """
    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    cargo_home.mkdir(parents=True, exist_ok=True)

    # Write credentials
    creds_file = cargo_home / "credentials.toml"
    creds_content = f'[registries.{registry_name}]\ntoken = "Bearer {token}"\n'
    creds_file.write_text(creds_content)
    creds_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    info(f"  Configured credentials for registry '{registry_name}'")

    # Ensure config has registry index
    config_file = cargo_home / "config.toml"
    registry_section = f"[registries.{registry_name}]"

    if config_file.exists():
        existing = config_file.read_text()
        if registry_section in existing:
            return
    else:
        existing = ""

    config_entry = f'\n{registry_section}\nindex = "{index_url}"\n'
    config_file.write_text(existing + config_entry)
    info(f"  Configured registry index: {index_url}")


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


def _publish_jfrog() -> int:
    """Publish to JFrog Artifactory Cargo registry.

    Follows the old CI pattern (ci/scripts/languages/rust/publish.sh):
    - Configures credentials.toml with Bearer token
    - Configures config.toml with sparse index URL
    - Runs cargo publish with --allow-dirty
    - Handles "already exists" gracefully

    Requires JFROG_TOKEN env var and uses org config for repository URLs.

    Returns:
        Exit code (0 = success).

    """
    token = os.environ.get("JFROG_TOKEN")
    if not token:
        error("JFROG_TOKEN not set — cannot publish to JFrog")
        return 1

    mask(token)

    org = load_org_config()
    registry_name = "hyperi"
    index_url = (
        f"sparse+https://{org.jfrog_domain}/artifactory"
        f"/api/cargo/{org.jfrog_org_prefix}-cargo-virtual/index/"
    )

    _configure_jfrog_registry(registry_name, index_url, token)

    info(f"  Publishing to registry '{registry_name}'...")
    result = subprocess.run(
        [
            "cargo",
            "publish",
            "--registry",
            registry_name,
            "--allow-dirty",
            # --no-verify: CI build step already verified; publish runner lacks
            # native build tools (e.g. protoc) needed by build scripts.
            "--no-verify",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        if "already exists" in result.stderr:
            warn("  Crate version already exists on JFrog registry (skipping)")
            return 0
        error("JFrog Cargo publish failed")
        if result.stderr:
            error(result.stderr)
        return result.returncode

    success("Published to JFrog Cargo registry")
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Rust publish stage.

    For library crates: reads VERSION file, syncs to Cargo.toml, publishes
    to configured cargo registries (crates.io, JFrog).

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

        elif dest == "jfrog-cargo":
            with group("Publish: JFrog Cargo"):
                rc = _publish_jfrog()
                if rc != 0:
                    return rc

        else:
            error(f"Unknown Rust publish destination: {dest}")
            return 1

    return 0
