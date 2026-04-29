# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/publish.py
# Purpose:   Python publish handler (PyPI + JFrog)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python publish handler.

Publishes Python packages to PyPI (OSS) and/or JFrog Artifactory (internal)
depending on the publish target configuration.
"""

from __future__ import annotations

import os
import subprocess

from hyperi_ci.common import error, group, info, success, warn
from hyperi_ci.config import CIConfig, load_org_config


def _publish_pypi() -> int:
    """Publish to PyPI using OIDC trusted publishing.

    Requires PYPI_TOKEN env var or OIDC trust configured in PyPI.

    Returns:
        Exit code (0 = success).

    """
    cmd = ["uv", "publish"]

    token = os.environ.get("PYPI_TOKEN")
    if token:
        cmd.extend(["--token", token])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if "already exists" in (result.stderr + result.stdout):
            warn("  Package version already exists on PyPI (skipping)")
            return 0
        error("PyPI publish failed")
        if result.stderr:
            error(result.stderr)
        return result.returncode

    success("Published to PyPI")
    return 0


def _publish_jfrog() -> int:
    """Publish to JFrog Artifactory PyPI repository.

    Requires JFROG_TOKEN env var and uses org config for repository URL.

    Returns:
        Exit code (0 = success).

    """
    token = os.environ.get("JFROG_TOKEN")
    if not token:
        error("JFROG_TOKEN not set — cannot publish to JFrog")
        return 1

    org = load_org_config()

    # JFrog Artifactory uses --username / --password, NOT --token.
    # The PyPI __token__ username is not supported by JFrog.
    # JFROG_USERNAME should be the JFrog account username/email.
    # Falls back to "_token" which works with JFrog Platform access tokens.
    username = os.environ.get("JFROG_USERNAME", "_token")
    cmd = [
        "uv",
        "publish",
        "--publish-url",
        org.pypi_publish_url,
        "--username",
        username,
        "--password",
        token,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if "already exists" in (result.stderr + result.stdout):
            warn("  Package version already exists on JFrog PyPI (skipping)")
            return 0
        error("JFrog PyPI publish failed")
        if result.stderr:
            error(result.stderr)
        return result.returncode

    success("Published to JFrog PyPI")
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Python publish stage.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).

    """
    destinations = config.destination_for("python")
    if not destinations:
        info("No Python publish destinations configured")
        return 0

    info(f"Publishing Python package to: {', '.join(destinations)}")

    for dest in destinations:
        if dest == "pypi":
            with group("Publish: PyPI"):
                rc = _publish_pypi()
                if rc != 0:
                    return rc

        elif dest == "jfrog-pypi":
            with group("Publish: JFrog PyPI"):
                rc = _publish_jfrog()
                if rc != 0:
                    return rc

        else:
            error(f"Unknown Python publish destination: {dest}")
            return 1

    return 0
