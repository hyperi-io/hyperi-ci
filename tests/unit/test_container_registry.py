# Project:   HyperI CI
# File:      tests/unit/test_container_registry.py
# Purpose:   Tests for publish.target -> registry base resolution
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from hyperi_ci.config import OrgConfig
from hyperi_ci.container.registry import resolve_registry_bases


@pytest.fixture
def org() -> OrgConfig:
    return OrgConfig()


def test_oss_resolves_to_ghcr_only(org: OrgConfig) -> None:
    assert resolve_registry_bases(target="oss", org=org) == ["ghcr.io/hyperi-io"]


def test_legacy_target_internal_routes_to_ghcr(org: OrgConfig) -> None:
    """`target: internal` is accepted for back-compat but ignored —
    every container publishes to GHCR. JFrog was removed in v2.1.4.
    """
    assert resolve_registry_bases(target="internal", org=org) == ["ghcr.io/hyperi-io"]


def test_legacy_target_both_routes_to_ghcr(org: OrgConfig) -> None:
    """`target: both` is accepted for back-compat but treated as OSS."""
    assert resolve_registry_bases(target="both", org=org) == ["ghcr.io/hyperi-io"]


def test_unknown_target_routes_to_ghcr(org: OrgConfig) -> None:
    """Unknown values are ignored — every container publishes to GHCR."""
    assert resolve_registry_bases(target="dockerhub", org=org) == ["ghcr.io/hyperi-io"]


def test_resolution_uses_org_overrides() -> None:
    """Custom org config (e.g. forks running their own GHCR) is honoured."""
    custom = OrgConfig(github_org="example-co", ghcr_org="example-co")
    assert resolve_registry_bases(target="oss", org=custom) == ["ghcr.io/example-co"]
