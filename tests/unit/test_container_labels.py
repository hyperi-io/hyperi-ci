# Project:   HyperI CI
# File:      tests/unit/test_container_labels.py
# Purpose:   Tests for OCI image label generation
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for OCI-standard label generation."""

from __future__ import annotations

from hyperi_ci.container.labels import build_oci_labels, labels_to_build_args


def test_build_oci_labels_basic() -> None:
    labels = build_oci_labels(
        repo="hyperi-io/my-app",
        revision="abc1234",
        version="1.2.3",
        title="My App",
        description="A test application",
    )

    assert (
        labels["org.opencontainers.image.source"]
        == "https://github.com/hyperi-io/my-app"
    )
    assert labels["org.opencontainers.image.revision"] == "abc1234"
    assert labels["org.opencontainers.image.version"] == "1.2.3"
    assert labels["org.opencontainers.image.title"] == "My App"
    assert labels["org.opencontainers.image.description"] == "A test application"
    assert labels["org.opencontainers.image.vendor"] == "HYPERI PTY LIMITED"
    assert labels["org.opencontainers.image.licenses"] == "FSL-1.1-ALv2"
    assert labels["io.hyperi.profile"] == "production"
    # created label must be a non-empty ISO 8601 UTC string
    assert labels["org.opencontainers.image.created"].endswith("Z")
    assert len(labels["org.opencontainers.image.created"]) == 20


def test_build_oci_labels_with_extras() -> None:
    extra = {"com.example.team": "platform", "com.example.env": "prod"}
    labels = build_oci_labels(
        repo="hyperi-io/my-app",
        revision="def5678",
        version="2.0.0",
        title="My App",
        extra_labels=extra,
    )

    assert labels["com.example.team"] == "platform"
    assert labels["com.example.env"] == "prod"
    # Standard labels still present
    assert labels["org.opencontainers.image.version"] == "2.0.0"


def test_labels_to_build_args() -> None:
    labels = {
        "org.opencontainers.image.version": "1.0.0",
        "org.opencontainers.image.title": "App",
        "io.hyperi.profile": "production",
    }

    args = labels_to_build_args(labels)

    # Must be flat list of alternating --label and key=value
    assert args[0] == "--label"
    # Rebuild expectation: sorted keys
    sorted_keys = sorted(labels)
    expected: list[str] = []
    for key in sorted_keys:
        expected.append("--label")
        expected.append(f"{key}={labels[key]}")

    assert args == expected
