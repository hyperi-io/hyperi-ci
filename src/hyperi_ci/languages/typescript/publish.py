# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/publish.py
# Purpose:   TypeScript/Node publish handler (npm + GitHub Packages)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript/Node publish handler — publishes npm packages to npmjs.com or GitHub Packages."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hyperi_ci.common import error, group, info, success, warn
from hyperi_ci.config import CIConfig, load_org_config


def _publish_npm() -> int:
    """Publish to npmjs.com.

    Requires NPM_TOKEN env var or npm OIDC trust.

    Returns:
        Exit code (0 = success).

    """
    token = os.environ.get("NPM_TOKEN")
    if not token:
        error("NPM_TOKEN not set — cannot publish to npm")
        return 1

    result = subprocess.run(
        ["npm", "publish", "--access", "public"],
        env={**os.environ, "NPM_TOKEN": token},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "already exists" in (result.stderr + result.stdout):
            warn("  Package version already exists on npm (skipping)")
            return 0
        error("npm publish failed")
        if result.stderr:
            error(result.stderr)
        return result.returncode

    success("Published to npm")
    return 0


def _publish_ghcr_npm() -> int:
    """Publish to GitHub Packages npm registry.

    Uses GITHUB_TOKEN (via GH_TOKEN) for auth. Packages are private by default
    and visible only to org members.

    Returns:
        Exit code (0 = success).

    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        error("GH_TOKEN/GITHUB_TOKEN not set — cannot publish to GitHub Packages")
        return 1

    org = load_org_config()
    registry_url = f"https://npm.pkg.github.com/{org.github_org}"

    npmrc = Path(".npmrc")
    npmrc_backup = npmrc.read_text() if npmrc.exists() else None

    npmrc.write_text(
        f"@{org.github_org}:registry={registry_url}\n"
        f"//npm.pkg.github.com/:_authToken={token}\n"
    )

    try:
        result = subprocess.run(["npm", "publish"], capture_output=True, text=True)
    finally:
        if npmrc_backup is not None:
            npmrc.write_text(npmrc_backup)
        else:
            npmrc.unlink(missing_ok=True)

    if result.returncode != 0:
        if "already exists" in (result.stderr + result.stdout):
            warn("  Package version already exists on GitHub Packages (skipping)")
            return 0
        error("GitHub Packages npm publish failed")
        if result.stderr:
            error(result.stderr)
        return result.returncode

    success("Published to GitHub Packages npm")
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run TypeScript/Node publish stage.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).

    """
    destinations = config.destination_for("npm")
    if not destinations:
        info("No npm publish destinations configured")
        return 0

    info(f"Publishing npm package to: {', '.join(destinations)}")

    for dest in destinations:
        if dest == "npmjs":
            with group("Publish: npm"):
                rc = _publish_npm()
                if rc != 0:
                    return rc

        elif dest == "ghcr-npm":
            with group("Publish: GitHub Packages npm"):
                rc = _publish_ghcr_npm()
                if rc != 0:
                    return rc

        else:
            error(f"Unknown npm publish destination: {dest}")
            return 1

    return 0
