# Project:   HyperI CI
# File:      tests/unit/test_container_build.py
# Purpose:   Tests for container tag resolution
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from hyperi_ci.container.build import resolve_tags


def test_resolve_tags_validate_mode_returns_no_tags():
    """Validate mode (push-to-main / local) — no tags land in any registry."""
    tags = resolve_tags(
        registry_bases=["ghcr.io/hyperi-io"],
        image_name="dfe-loader",
        version="1.13.5",
        sha="abc1234",
        mode="validate",
    )
    assert tags == []


class TestDevModeTags:
    """Branch-mode dev images (plan decision 3): mutable branch pointer +
    immutable branch-scoped sha pin, and NEVER a version tag, latest, or a
    bare sha-<short> — those are the GA artifact class, published only
    from main. The distinct branch-*/dev-sha-* prefixes are load-bearing:
    the scheduled GHCR pruner globs them without touching GA pins."""

    def test_dev_mode_branch_pointer_and_pin_tags(self):
        tags = resolve_tags(
            registry_bases=["ghcr.io/hyperi-io"],
            image_name="dfe-loader",
            version="1.13.5",
            sha="abc1234",
            mode="dev",
            branch_slug="fix-plan-permissions",
        )
        assert tags == [
            "ghcr.io/hyperi-io/dfe-loader:branch-fix-plan-permissions",
            "ghcr.io/hyperi-io/dfe-loader:branch-fix-plan-permissions-sha-abc1234",
        ]

    def test_dev_mode_never_emits_ga_namespace(self):
        tags = resolve_tags(
            registry_bases=["ghcr.io/hyperi-io"],
            image_name="dfe-loader",
            version="1.13.5",
            sha="abc1234",
            channel="release",
            mode="dev",
            branch_slug="anything",
        )
        joined = " ".join(tags)
        assert "v1.13.5" not in joined and "latest" not in joined
        # The bare sha-<short> namespace belongs to GA publishes — a dev
        # image emitting it would make prune globs unsafe.
        assert not any(t.endswith(":sha-abc1234") for t in tags)

    def test_dev_mode_empty_slug_falls_back_to_dev_sha(self):
        tags = resolve_tags(
            registry_bases=["ghcr.io/hyperi-io"],
            image_name="dfe-loader",
            version="1.13.5",
            sha="abc1234",
            mode="dev",
            branch_slug="",
        )
        assert tags == ["ghcr.io/hyperi-io/dfe-loader:dev-sha-abc1234"]


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
