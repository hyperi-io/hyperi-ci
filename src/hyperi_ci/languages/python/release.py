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
from collections.abc import Sequence

from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages._build_common import run_destinations


def publish_pypi(files: Sequence[str] = ()) -> int:
    """Publish to PyPI with PYPI_TOKEN, or OIDC trusted publishing when unset.

    Args:
        files: The distributions to upload. Empty uploads ``dist/*``.

    Returns:
        Exit code (0 = success).

    """
    token = os.environ.get("PYPI_TOKEN")
    result = run_cmd(
        ["uv", "publish", "--no-config", *files],
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

    return run_destinations(
        destinations, {"pypi": ("Publish: PyPI", publish_pypi)}, "Python"
    )
