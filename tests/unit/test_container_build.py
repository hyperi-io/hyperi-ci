# Project:   HyperI CI
# File:      tests/unit/test_container_build.py
# Purpose:   Tests for container tag resolution (build execution tested via integration)
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from hyperi_ci.container.build import resolve_tags


def test_resolve_tags_push_to_main():
    tags = resolve_tags(
        registry="ghcr.io/hyperi-io",
        image_name="dfe-loader",
        version="1.13.5",
        sha="abc1234",
        is_push_to_main=True,
    )
    assert tags == ["ghcr.io/hyperi-io/dfe-loader:sha-abc1234"]


def test_resolve_tags_release_channel():
    tags = resolve_tags(
        registry="ghcr.io/hyperi-io",
        image_name="dfe-loader",
        version="1.13.5",
        sha="abc1234",
        channel="release",
    )
    assert tags == [
        "ghcr.io/hyperi-io/dfe-loader:v1.13.5",
        "ghcr.io/hyperi-io/dfe-loader:latest",
    ]


def test_resolve_tags_alpha_channel():
    tags = resolve_tags(
        registry="ghcr.io/hyperi-io",
        image_name="dfe-loader",
        version="1.13.5",
        sha="abc1234",
        channel="alpha",
    )
    assert tags == ["ghcr.io/hyperi-io/dfe-loader:v1.13.5-alpha"]


def test_resolve_tags_spike_channel():
    tags = resolve_tags(
        registry="ghcr.io/hyperi-io",
        image_name="dfe-loader",
        version="1.0.0",
        sha="def5678",
        channel="spike",
    )
    assert tags == ["ghcr.io/hyperi-io/dfe-loader:v1.0.0-spike"]


def test_resolve_tags_beta_channel():
    tags = resolve_tags(
        registry="ghcr.io/hyperi-io",
        image_name="dfe-receiver",
        version="2.0.0",
        sha="aaa1111",
        channel="beta",
    )
    assert tags == ["ghcr.io/hyperi-io/dfe-receiver:v2.0.0-beta"]
