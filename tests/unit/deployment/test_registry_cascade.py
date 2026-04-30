# Project:   HyperI CI
# File:      tests/unit/deployment/test_registry_cascade.py
# Purpose:   Cascade-driven registry resolver tests
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.deployment.registry` cascade resolvers."""

from __future__ import annotations

from hyperi_ci.config import CIConfig
from hyperi_ci.deployment.registry import (
    DEFAULT_BASE_IMAGE,
    DEFAULT_IMAGE_REGISTRY,
    argocd_repo_url_from_cascade,
    base_image_from_cascade,
    image_registry_from_cascade,
)


def _config(**deployment: object) -> CIConfig:
    """Build a CIConfig with `deployment.*` keys preset.

    Saves callers from rebuilding the nested dict structure in every
    test. Pass dotted-style: `_config(image_registry="x")`.
    """
    raw: dict = {"deployment": {}}
    for key, value in deployment.items():
        if "." in key:
            head, _, tail = key.partition(".")
            raw["deployment"].setdefault(head, {})[tail] = value
        else:
            raw["deployment"][key] = value
    return CIConfig(_raw=raw)


class TestImageRegistryCascade:
    """Tests for :func:`image_registry_from_cascade`."""

    def test_default_when_unset(self) -> None:
        cfg = CIConfig()
        assert image_registry_from_cascade(cfg) == DEFAULT_IMAGE_REGISTRY

    def test_default_when_empty_string(self) -> None:
        # Empty-string overrides should fall through to the default,
        # not produce a malformed registry like "/foo".
        cfg = _config(image_registry="")
        assert image_registry_from_cascade(cfg) == DEFAULT_IMAGE_REGISTRY

    def test_explicit_override(self) -> None:
        cfg = _config(image_registry="harbor.devex.hyperi.io:8443/library")
        assert image_registry_from_cascade(cfg) == "harbor.devex.hyperi.io:8443/library"

    def test_non_string_falls_back(self) -> None:
        # Operator typo — passing a number — shouldn't crash; defaults are
        # the safe choice when the cascade has the wrong type.
        cfg = CIConfig(_raw={"deployment": {"image_registry": 42}})
        assert image_registry_from_cascade(cfg) == DEFAULT_IMAGE_REGISTRY


class TestBaseImageCascade:
    """Tests for :func:`base_image_from_cascade`."""

    def test_default_when_unset(self) -> None:
        cfg = CIConfig()
        assert base_image_from_cascade(cfg) == DEFAULT_BASE_IMAGE

    def test_explicit_override(self) -> None:
        cfg = _config(base_image="ghcr.io/hyperi-io/dfe-base:ubuntu-24.04")
        assert base_image_from_cascade(cfg) == "ghcr.io/hyperi-io/dfe-base:ubuntu-24.04"

    def test_empty_falls_back(self) -> None:
        cfg = _config(base_image="")
        assert base_image_from_cascade(cfg) == DEFAULT_BASE_IMAGE


class TestArgocdRepoUrlCascade:
    """Tests for :func:`argocd_repo_url_from_cascade`."""

    def test_default_uses_app_name(self) -> None:
        cfg = CIConfig()
        url = argocd_repo_url_from_cascade(cfg, "dfe-loader")
        assert url == "https://github.com/hyperi-io/dfe-loader"

    def test_explicit_override_ignores_app_name(self) -> None:
        cfg = _config(**{"argocd.repo_url": "https://gitlab.com/foo/bar"})
        url = argocd_repo_url_from_cascade(cfg, "dfe-loader")
        # Override wins — app_name is only used as a fallback formatter.
        assert url == "https://gitlab.com/foo/bar"

    def test_empty_falls_back_to_default(self) -> None:
        cfg = _config(**{"argocd.repo_url": ""})
        url = argocd_repo_url_from_cascade(cfg, "dfe-loader")
        assert url == "https://github.com/hyperi-io/dfe-loader"
