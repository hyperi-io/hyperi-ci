# Project:   HyperI CI
# File:      tests/unit/test_container_labels.py
# Purpose:   Tests for OCI image label generation
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for OCI-standard label generation."""

from pathlib import Path

import pytest
import yaml

from hyperi_ci import classification
from hyperi_ci.container.labels import build_oci_labels, labels_to_build_args
from hyperi_ci.init import find_license

_VENDOR = "org.opencontainers.image.vendor"
_LICENSES = "org.opencontainers.image.licenses"

# The labels every image carries whatever its classification, `created` aside.
_COMMON = {
    "org.opencontainers.image.source": "https://github.com/hyperi-io/my-app",
    "org.opencontainers.image.revision": "abc1234",
    "org.opencontainers.image.version": "1.2.3",
    "org.opencontainers.image.title": "My App",
    "org.opencontainers.image.description": "A test application",
    "io.hyperi.profile": "production",
    "io.hyperi.optimized": "true",
}


def _labels(*, classification: str = "", licenses: str | None = None) -> dict[str, str]:
    """Build labels for the fixed test image, with `created` checked and dropped."""
    labels = build_oci_labels(
        repo="hyperi-io/my-app",
        revision="abc1234",
        version="1.2.3",
        title="My App",
        description="A test application",
        classification=classification,
        licenses=licenses,
    )
    created = labels.pop("org.opencontainers.image.created")
    assert created.endswith("Z")
    assert len(created) == 20
    return labels


def _expected(*, vendor: str | None, licence: str | None) -> dict[str, str]:
    expected = dict(_COMMON)
    if vendor is not None:
        expected[_VENDOR] = vendor
    if licence is not None:
        expected[_LICENSES] = licence
    return expected


def test_hyperi_owned_labels_are_unchanged() -> None:
    """A HyperI-owned image keeps the exact labels, in the same order, as before."""
    before = [
        ("org.opencontainers.image.source", "https://github.com/hyperi-io/my-app"),
        ("org.opencontainers.image.revision", "abc1234"),
        ("org.opencontainers.image.version", "1.2.3"),
        ("org.opencontainers.image.title", "My App"),
        ("org.opencontainers.image.description", "A test application"),
        ("org.opencontainers.image.vendor", "HYPERI PTY LIMITED"),
        ("org.opencontainers.image.licenses", "BUSL-1.1"),
        ("io.hyperi.profile", "production"),
        ("io.hyperi.optimized", "true"),
    ]
    for category in ("internal", "product"):
        assert list(_labels(classification=category).items()) == before
        assert (
            list(_labels(classification=category, licenses="BUSL-1.1").items())
            == before
        )


@pytest.mark.parametrize(
    ("category", "licence", "vendor", "expected_licence"),
    [
        ("internal", None, "HYPERI PTY LIMITED", "BUSL-1.1"),
        ("internal", "Apache-2.0", "HYPERI PTY LIMITED", "Apache-2.0"),
        ("product", None, "HYPERI PTY LIMITED", "BUSL-1.1"),
        ("product", "Apache-2.0", "HYPERI PTY LIMITED", "Apache-2.0"),
        ("fork", None, None, None),
        ("fork", "MIT", None, "MIT"),
        ("general-oss", None, None, None),
        ("general-oss", "Apache-2.0", None, "Apache-2.0"),
        ("", None, None, None),
        ("", "BUSL-1.1", None, "BUSL-1.1"),
    ],
)
def test_vendor_and_licence_follow_classification(
    category: str,
    licence: str | None,
    vendor: str | None,
    expected_licence: str | None,
) -> None:
    labels = _labels(classification=category, licenses=licence)
    assert labels == _expected(vendor=vendor, licence=expected_licence)


def test_no_classification_claims_nothing() -> None:
    """An image built with no declared classification names no vendor or licence."""
    assert _labels() == _expected(vendor=None, licence=None)


def test_empty_licence_is_treated_as_none() -> None:
    assert _labels(classification="general-oss", licenses="") == _expected(
        vendor=None, licence=None
    )
    assert _labels(classification="product", licenses="") == _expected(
        vendor="HYPERI PTY LIMITED", licence="BUSL-1.1"
    )


def _repo_labels(project_dir: Path) -> dict[str, str]:
    """Labels for a repo, read the way the container stage reads them."""
    config_file = project_dir / ".hyperi-ci.yaml"
    raw = {}
    if config_file.exists():
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    declared = classification.resolve(raw, project_dir).value
    return _labels(classification=declared, licenses=find_license(project_dir))


_APACHE_TEXT = "Apache License\nVersion 2.0\nLicensed under the Apache"

# Declared spelling -> (vendor label, licence label when nothing is detected).
_BRANCHES = {
    "hyperi": ("HYPERI PTY LIMITED", "BUSL-1.1"),
    "product": ("HYPERI PTY LIMITED", "BUSL-1.1"),
    "fork": (None, None),
    "oss": (None, None),
    None: (None, None),
}


def _write_repo(project_dir: Path, declared: str | None, extra: str = "") -> None:
    lines = ["language: python"]
    if declared is not None:
        lines.append(f"classification: {declared}")
    if extra:
        lines.append(extra)
    (project_dir / ".hyperi-ci.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


@pytest.mark.parametrize("declared", list(_BRANCHES))
def test_repo_with_no_detectable_licence(tmp_path: Path, declared: str | None) -> None:
    _write_repo(tmp_path, declared)
    vendor, fallback = _BRANCHES[declared]
    assert _repo_labels(tmp_path) == _expected(vendor=vendor, licence=fallback)


@pytest.mark.parametrize("declared", list(_BRANCHES))
def test_repo_with_detected_licence(tmp_path: Path, declared: str | None) -> None:
    _write_repo(tmp_path, declared)
    (tmp_path / "LICENSE").write_text(_APACHE_TEXT, encoding="utf-8")
    vendor, _ = _BRANCHES[declared]
    assert _repo_labels(tmp_path) == _expected(vendor=vendor, licence="Apache-2.0")


@pytest.mark.parametrize("declared", list(_BRANCHES))
def test_explicit_licence_beats_detection(tmp_path: Path, declared: str | None) -> None:
    _write_repo(tmp_path, declared, extra="license: MIT")
    (tmp_path / "LICENSE").write_text(_APACHE_TEXT, encoding="utf-8")
    vendor, _ = _BRANCHES[declared]
    assert _repo_labels(tmp_path) == _expected(vendor=vendor, licence="MIT")


def test_dotfile_classification_is_read(tmp_path: Path) -> None:
    (tmp_path / ".hyperi-classification").write_text("oss\n", encoding="utf-8")
    assert _repo_labels(tmp_path) == _expected(vendor=None, licence=None)


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


def test_optimized_defaults_to_true() -> None:
    labels = build_oci_labels(
        repo="hyperi-io/app",
        revision="abc123",
        version="1.2.3",
        title="My App",
    )
    assert labels["io.hyperi.optimized"] == "true"


def test_a_skipped_optimisation_is_stamped_on_the_image() -> None:
    """A fast deploy-and-test image must be identifiable without benchmarking."""
    labels = build_oci_labels(
        repo="hyperi-io/app",
        revision="abc123",
        version="1.2.3",
        title="My App",
        optimized=False,
    )
    assert labels["io.hyperi.optimized"] == "false"
