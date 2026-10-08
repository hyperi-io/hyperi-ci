# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/release.py
# Purpose:   Python release handler (PyPI)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python release handler: uploads the Build job's wheel and sdist to PyPI.

``uv publish`` runs with ``--no-config`` so a project's ``[tool.uv] publish-url``
cannot redirect the token (issue #409). The token goes in the environment, as
the command line is readable by any process on the runner.
"""

import os

from hyperi_ci.common import error, group, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig


def _publish_pypi() -> int:
    """Publish to PyPI with PYPI_TOKEN, or OIDC trusted publishing when unset.

    Returns:
        Exit code (0 = success).

    """
    token = os.environ.get("PYPI_TOKEN")
    result = run_cmd(
        ["uv", "publish", "--no-config"],
        env={"UV_PUBLISH_TOKEN": token} if token else None,
        capture=True,
        check=False,
    )
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
