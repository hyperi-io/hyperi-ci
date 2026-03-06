# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/publish.py
# Purpose:   Rust publish handler (crates.io + JFrog)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust publish handler.

Publishes Rust crates to crates.io (OSS) and/or JFrog Artifactory (internal)
depending on the publish target configuration.
"""

from __future__ import annotations

import os
import subprocess

from hyperi_ci.common import error, group, info, success
from hyperi_ci.config import CIConfig, load_org_config


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
        ["cargo", "publish", "--no-verify"],
        env={**os.environ, "CARGO_REGISTRY_TOKEN": token},
    )
    if result.returncode != 0:
        error("crates.io publish failed")
        return result.returncode

    success("Published to crates.io")
    return 0


def _publish_jfrog() -> int:
    """Publish to JFrog Artifactory Cargo repository.

    Requires JFROG_TOKEN env var and uses org config for repository URL.

    Returns:
        Exit code (0 = success).
    """
    token = os.environ.get("JFROG_TOKEN")
    if not token:
        error("JFROG_TOKEN not set — cannot publish to JFrog")
        return 1

    org = load_org_config()

    result = subprocess.run(
        [
            "cargo",
            "publish",
            "--no-verify",
            "--registry",
            "jfrog",
        ],
        env={
            **os.environ,
            "CARGO_REGISTRIES_JFROG_INDEX": org.cargo_url,
            "CARGO_REGISTRIES_JFROG_TOKEN": token,
        },
    )
    if result.returncode != 0:
        error("JFrog Cargo publish failed")
        return result.returncode

    success("Published to JFrog Cargo")
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Rust publish stage.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).
    """
    destinations = config.destination_for("cargo")
    if not destinations:
        info("No Rust publish destinations configured")
        return 0

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
