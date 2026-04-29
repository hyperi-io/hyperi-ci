# Project:   HyperI CI
# File:      tests/unit/test_container_build.py
# Purpose:   Tests for container tag resolution
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from hyperi_ci.container.build import resolve_tags


def test_resolve_tags_push_to_main_returns_no_tags():
    """Push-to-main runs in validate mode — no tags are written to any registry."""
    tags = resolve_tags(
        registry_bases=["ghcr.io/hyperi-io"],
        image_name="dfe-loader",
        version="1.13.5",
        sha="abc1234",
        is_push_to_main=True,
    )
    assert tags == []


def test_resolve_tags_release_channel_includes_sha():
    tags = resolve_tags(
        registry_bases=["ghcr.io/hyperi-io"],
        image_name="dfe-loader",
        version="1.13.5",
        sha="abc1234",
        channel="release",
    )
    assert tags == [
        "ghcr.io/hyperi-io/dfe-loader:v1.13.5",
        "ghcr.io/hyperi-io/dfe-loader:latest",
        "ghcr.io/hyperi-io/dfe-loader:sha-abc1234",
    ]


def test_resolve_tags_pre_ga_channel_includes_sha():
    tags = resolve_tags(
        registry_bases=["ghcr.io/hyperi-io"],
        image_name="dfe-loader",
        version="1.13.5",
        sha="abc1234",
        channel="alpha",
    )
    assert tags == [
        "ghcr.io/hyperi-io/dfe-loader:v1.13.5-alpha",
        "ghcr.io/hyperi-io/dfe-loader:sha-abc1234",
    ]


def test_resolve_tags_multi_registry_release():
    tags = resolve_tags(
        registry_bases=[
            "ghcr.io/hyperi-io",
            "hypersec.jfrog.io/hyperi-docker-local",
        ],
        image_name="dfe-loader",
        version="1.13.5",
        sha="abc1234",
        channel="release",
    )
    assert tags == [
        "ghcr.io/hyperi-io/dfe-loader:v1.13.5",
        "ghcr.io/hyperi-io/dfe-loader:latest",
        "ghcr.io/hyperi-io/dfe-loader:sha-abc1234",
        "hypersec.jfrog.io/hyperi-docker-local/dfe-loader:v1.13.5",
        "hypersec.jfrog.io/hyperi-docker-local/dfe-loader:latest",
        "hypersec.jfrog.io/hyperi-docker-local/dfe-loader:sha-abc1234",
    ]


def test_resolve_tags_multi_registry_pre_ga():
    tags = resolve_tags(
        registry_bases=[
            "ghcr.io/hyperi-io",
            "hypersec.jfrog.io/hyperi-docker-local",
        ],
        image_name="dfe-receiver",
        version="2.0.0",
        sha="aaa1111",
        channel="beta",
    )
    assert tags == [
        "ghcr.io/hyperi-io/dfe-receiver:v2.0.0-beta",
        "ghcr.io/hyperi-io/dfe-receiver:sha-aaa1111",
        "hypersec.jfrog.io/hyperi-docker-local/dfe-receiver:v2.0.0-beta",
        "hypersec.jfrog.io/hyperi-docker-local/dfe-receiver:sha-aaa1111",
    ]


def test_resolve_tags_spike_and_beta_channels():
    spike = resolve_tags(
        registry_bases=["ghcr.io/hyperi-io"],
        image_name="dfe-loader",
        version="1.0.0",
        sha="def5678",
        channel="spike",
    )
    assert spike == [
        "ghcr.io/hyperi-io/dfe-loader:v1.0.0-spike",
        "ghcr.io/hyperi-io/dfe-loader:sha-def5678",
    ]

    beta = resolve_tags(
        registry_bases=["ghcr.io/hyperi-io"],
        image_name="dfe-receiver",
        version="2.0.0",
        sha="aaa1111",
        channel="beta",
    )
    assert beta == [
        "ghcr.io/hyperi-io/dfe-receiver:v2.0.0-beta",
        "ghcr.io/hyperi-io/dfe-receiver:sha-aaa1111",
    ]
