# Project:   HyperI CI
# File:      tests/unit/test_container_registry.py
# Purpose:   Tests for container registry base resolution
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

from hyperi_ci.config import OrgConfig
from hyperi_ci.container.registry import resolve_registry_bases


def test_resolves_to_ghcr_only() -> None:
    assert resolve_registry_bases(org=OrgConfig()) == ["ghcr.io/hyperi-io"]


def test_resolution_uses_org_overrides() -> None:
    """Custom org config (e.g. forks running their own GHCR) is honoured."""
    custom = OrgConfig(github_org="example-co", ghcr_org="example-co")
    assert resolve_registry_bases(org=custom) == ["ghcr.io/example-co"]
