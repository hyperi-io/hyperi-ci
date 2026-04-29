# Project:   HyperI CI
# File:      src/hyperi_ci/container/registry.py
# Purpose:   Resolve publish.target into concrete container registry bases
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container registry resolution.

`publish.target` is the single source of truth for where a project's
artefacts go. For containers:

  oss      -> ghcr.io/<github-org>
  internal -> <jfrog-domain>/<jfrog-prefix>-docker-local
  both     -> both of the above

The repo-local `.hyperi-ci.yaml` no longer carries a `registry:` field;
routing is fully derived from `publish.target` to keep parity with how
PyPI / crates / npm / R2 are routed.

Docker Hub is intentionally NOT a target. The Docker Hub login step in
the reusable workflows remains, gated on `vars.DOCKERHUB_USERNAME`, so
authenticated pulls bypass anonymous rate limits — but no project
publishes to Docker Hub.
"""

from __future__ import annotations

from hyperi_ci.config import OrgConfig

_VALID_TARGETS = ("oss", "internal", "both")


def resolve_registry_bases(*, target: str, org: OrgConfig) -> list[str]:
    """Return the list of registry bases to push to.

    Args:
        target: ``publish.target`` value (``oss`` | ``internal`` | ``both``).
        org: Loaded organisation config.

    Returns:
        Ordered list of registry base URLs (no trailing slash, no image name).
        ``oss``      -> ``[ghcr.io/<org>]``
        ``internal`` -> ``[<jfrog-domain>/<prefix>-docker-local]``
        ``both``     -> both, in the order GHCR-first.

    Raises:
        ValueError: If ``target`` is not one of the recognised values.

    """
    if target not in _VALID_TARGETS:
        raise ValueError(
            f"publish.target must be one of {_VALID_TARGETS}, got {target!r}"
        )

    ghcr = f"{org.ghcr_registry}/{org.ghcr_org}"
    jfrog = org.docker_registry

    if target == "oss":
        return [ghcr]
    if target == "internal":
        return [jfrog]
    return [ghcr, jfrog]
