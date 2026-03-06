# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/publish.py
# Purpose:   TypeScript/Node publish handler (npm + JFrog)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript/Node publish handler.

Publishes npm packages to npmjs.com (OSS) and/or JFrog Artifactory (internal)
depending on the publish target configuration.
"""

from __future__ import annotations

import os
import subprocess

from hyperi_ci.common import error, group, info, success
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
    )
    if result.returncode != 0:
        error("npm publish failed")
        return result.returncode

    success("Published to npm")
    return 0


def _publish_jfrog() -> int:
    """Publish to JFrog Artifactory npm repository.

    Requires JFROG_TOKEN env var and uses org config for registry URL.

    Returns:
        Exit code (0 = success).
    """
    token = os.environ.get("JFROG_TOKEN")
    if not token:
        error("JFROG_TOKEN not set — cannot publish to JFrog")
        return 1

    org = load_org_config()

    # Configure npm to use JFrog registry with auth
    registry_url = org.npm_url
    subprocess.run(
        ["npm", "config", "set", f"registry={registry_url}"],
        check=False,
    )
    subprocess.run(
        [
            "npm",
            "config",
            "set",
            f"//{org.jfrog_domain}/artifactory/api/npm/"
            f"{org.jfrog_org_prefix}-npm/:_authToken={token}",
        ],
        check=False,
    )

    result = subprocess.run(["npm", "publish"])
    if result.returncode != 0:
        error("JFrog npm publish failed")
        return result.returncode

    success("Published to JFrog npm")
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

        elif dest == "jfrog-npm":
            with group("Publish: JFrog npm"):
                rc = _publish_jfrog()
                if rc != 0:
                    return rc

        else:
            error(f"Unknown npm publish destination: {dest}")
            return 1

    return 0
