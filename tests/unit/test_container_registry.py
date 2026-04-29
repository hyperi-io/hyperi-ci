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


def test_internal_resolves_to_jfrog_only(org: OrgConfig) -> None:
    assert resolve_registry_bases(target="internal", org=org) == [
        "hypersec.jfrog.io/hyperi-docker-local",
    ]


def test_both_resolves_to_ghcr_then_jfrog(org: OrgConfig) -> None:
    assert resolve_registry_bases(target="both", org=org) == [
        "ghcr.io/hyperi-io",
        "hypersec.jfrog.io/hyperi-docker-local",
    ]


def test_invalid_target_raises(org: OrgConfig) -> None:
    with pytest.raises(ValueError, match="publish.target must be one of"):
        resolve_registry_bases(target="dockerhub", org=org)


def test_resolution_uses_org_overrides() -> None:
    """Custom org config (e.g. forks running their own JFrog/GHCR) honoured."""
    custom = OrgConfig(
        github_org="example-co",
        ghcr_org="example-co",
        jfrog_domain="artifactory.example.co",
        jfrog_org_prefix="example",
    )
    assert resolve_registry_bases(target="oss", org=custom) == ["ghcr.io/example-co"]
    assert resolve_registry_bases(target="internal", org=custom) == [
        "artifactory.example.co/example-docker-local",
    ]
    assert resolve_registry_bases(target="both", org=custom) == [
        "ghcr.io/example-co",
        "artifactory.example.co/example-docker-local",
    ]
