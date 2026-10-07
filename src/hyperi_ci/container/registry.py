# Project:   HyperI CI
# File:      src/hyperi_ci/container/registry.py
# Purpose:   Resolve the container registry bases to push to
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container registry resolution.

Every container publishes to GHCR (``ghcr.io/<github-org>``).

Docker Hub is intentionally NOT a target. The Container job logs in to it,
gated on ``vars.DOCKERHUB_USERNAME``, only to authenticate the Dockerfile's
base-image pulls.
"""

from hyperi_ci.config import OrgConfig


def resolve_registry_bases(*, org: OrgConfig) -> list[str]:
    """Return the list of registry bases to push to.

    Args:
        org: Loaded organisation config.

    Returns:
        Always ``[ghcr.io/<org>]``.

    """
    return [f"{org.ghcr_registry}/{org.ghcr_org}"]
