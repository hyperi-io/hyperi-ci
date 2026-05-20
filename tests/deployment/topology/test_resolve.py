"""Tests for OCI chart version resolution."""

from __future__ import annotations

import pytest
from hyperi_pylib.deployment.topology.errors import VersionResolutionError

from hyperi_ci.deployment.topology.resolve import (
    ChartVersionResolver,
)


def test_resolver_picks_highest_matching_version():
    # ^X.Y means >=X.Y, <(X+1).0 — highest within that major band wins
    resolver = ChartVersionResolver(
        registry="oci://ghcr.io/hyperi-io/helm-charts",
        available={"dfe-loader": ["1.18.0", "1.18.3", "1.19.0", "2.0.0"]},
    )
    # ^1.18 → >=1.18, <2.0 → highest is 1.19.0
    assert resolver.resolve("dfe-loader", "^1.18") == "1.19.0"
    # ^1.0 → >=1.0, <2.0 → highest is 1.19.0
    assert resolver.resolve("dfe-loader", "^1.0") == "1.19.0"
    # ^2.0 → >=2.0, <3.0 → only 2.0.0
    assert resolver.resolve("dfe-loader", "^2.0") == "2.0.0"


def test_resolver_exact_pin_passes_through():
    resolver = ChartVersionResolver(
        registry="oci://ghcr.io/hyperi-io/helm-charts",
        available={"dfe-loader": ["1.18.0", "1.18.3", "1.19.0"]},
    )
    assert resolver.resolve("dfe-loader", "1.18.0") == "1.18.0"


def test_resolver_unknown_chart_raises():
    resolver = ChartVersionResolver(
        registry="oci://ghcr.io/hyperi-io/helm-charts",
        available={},
    )
    with pytest.raises(VersionResolutionError) as ei:
        resolver.resolve("dfe-loader", "^1.0")
    assert ei.value.chart == "dfe-loader"
    assert ei.value.version_range == "^1.0"


def test_resolver_no_matching_version_raises():
    resolver = ChartVersionResolver(
        registry="oci://ghcr.io/hyperi-io/helm-charts",
        available={"dfe-loader": ["1.0.0", "1.5.0"]},
    )
    with pytest.raises(VersionResolutionError) as ei:
        resolver.resolve("dfe-loader", "^2.0")
    assert "no version" in str(ei.value).lower() or "no match" in str(ei.value).lower()


def test_resolver_ignores_prerelease_by_default():
    resolver = ChartVersionResolver(
        registry="oci://ghcr.io/hyperi-io/helm-charts",
        available={"dfe-loader": ["1.18.0", "1.19.0-rc.1", "1.18.3"]},
    )
    assert resolver.resolve("dfe-loader", "^1.0") == "1.18.3"
