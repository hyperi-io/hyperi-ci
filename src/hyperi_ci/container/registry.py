# Project:   HyperI CI
# File:      src/hyperi_ci/container/registry.py
# Purpose:   Resolve publish.target into concrete container registry bases
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container registry resolution.

Every container publishes to GHCR (``ghcr.io/<github-org>``). The legacy
``publish.target`` config key is accepted for backward compatibility with
downstream ``.hyperi-ci.yaml`` files but ignored at runtime — JFrog
publishing was removed in v2.1.4.

Docker Hub is intentionally NOT a target. The Docker Hub login step in
the reusable workflows remains, gated on ``vars.DOCKERHUB_USERNAME``, so
authenticated pulls bypass anonymous rate limits — but no project
publishes to Docker Hub.
"""

from __future__ import annotations

from hyperi_ci.config import OrgConfig


def resolve_registry_bases(*, target: str, org: OrgConfig) -> list[str]:
    """Return the list of registry bases to push to.

    Args:
        target: Legacy ``publish.target`` value, accepted but ignored.
        org: Loaded organisation config.

    Returns:
        Always ``[ghcr.io/<org>]``. The ``target`` argument is retained
        for back-compat with callers that still pass it.

    """
    del target  # ignored — every publish goes to GHCR
    return [f"{org.ghcr_registry}/{org.ghcr_org}"]
