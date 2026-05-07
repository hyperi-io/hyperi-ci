# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/publish.py
# Purpose:   Python publish handler (PyPI)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python publish handler — publishes Python packages to PyPI."""

from __future__ import annotations

import os
import subprocess

from hyperi_ci.common import error, group, info, success, warn
from hyperi_ci.config import CIConfig


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

        else:
            error(f"Unknown Python publish destination: {dest}")
            return 1

    return 0
