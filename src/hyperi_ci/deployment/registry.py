# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/registry.py
# Purpose:   Cascade-driven defaults for image_registry / base_image / argocd repo
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve container-registry, base-image, and ArgoCD repo from the cascade.

Mirrors `hyperi-rustlib::deployment::registry`. Org-wide defaults (where
images go, what they're built FROM, where ArgoCD looks) are not
per-app concerns — they live in the YAML cascade so ops can flip them
once for everyone.

Cascade keys (set in defaults.yaml or .hyperi-ci.yaml):

    deployment:
      image_registry: ghcr.io/hyperi-io
      base_image: ubuntu:24.04
      argocd:
        repo_url: https://github.com/hyperi-io/{app}
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hyperi_ci.deployment.contract import (
    DEFAULT_BASE_IMAGE,
    DEFAULT_IMAGE_REGISTRY,
)

if TYPE_CHECKING:
    from hyperi_ci.config import CIConfig

__all__ = [
    "DEFAULT_BASE_IMAGE",
    "DEFAULT_IMAGE_REGISTRY",
    "argocd_repo_url_from_cascade",
    "base_image_from_cascade",
    "image_registry_from_cascade",
]


def image_registry_from_cascade(config: CIConfig) -> str:
    """Return the publish-target image registry from the cascade.

    Reads ``deployment.image_registry`` from the merged YAML cascade.
    Falls back to :data:`DEFAULT_IMAGE_REGISTRY` (``ghcr.io/hyperi-io``)
    when unset or empty.

    Mirrors ``hyperi-rustlib::deployment::registry::image_registry_from_cascade``
    — same key, same fallback. Apps that delegate to this resolver get the
    same answer in Rust and Python.

    Args:
        config: The merged CIConfig (from :func:`hyperi_ci.config.load_config`).

    Returns:
        Registry base URL (e.g. ``ghcr.io/hyperi-io``). Never empty.

    """
    value = config.get("deployment.image_registry")
    if isinstance(value, str) and value:
        return value
    return DEFAULT_IMAGE_REGISTRY


def base_image_from_cascade(config: CIConfig) -> str:
    """Return the runtime-stage base image from the cascade.

    Reads ``deployment.base_image`` from the merged YAML cascade.
    Falls back to :data:`DEFAULT_BASE_IMAGE` (``ubuntu:24.04``) when
    unset or empty.

    Mirrors ``hyperi-rustlib::deployment::registry::base_image_from_cascade``.

    Args:
        config: The merged CIConfig.

    Returns:
        Base image reference (e.g. ``ubuntu:24.04``). Never empty.

    """
    value = config.get("deployment.base_image")
    if isinstance(value, str) and value:
        return value
    return DEFAULT_BASE_IMAGE


def argocd_repo_url_from_cascade(config: CIConfig, app_name: str) -> str:
    """Return the git repo URL ArgoCD should track for this app.

    Reads ``deployment.argocd.repo_url`` from the cascade. Falls back to
    ``https://github.com/hyperi-io/{app_name}`` — matches the org
    convention where each app lives under hyperi-io.

    Mirrors ``hyperi-rustlib::deployment::registry::argocd_repo_url_from_cascade``.

    Args:
        config: The merged CIConfig.
        app_name: Application name (used in the fallback URL only).

    Returns:
        Repo URL (e.g. ``https://github.com/hyperi-io/dfe-loader``).

    """
    value = config.get("deployment.argocd.repo_url")
    if isinstance(value, str) and value:
        return value
    return f"https://github.com/hyperi-io/{app_name}"
