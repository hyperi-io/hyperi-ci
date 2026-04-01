# Project:   HyperI CI
# File:      src/hyperi_ci/container/labels.py
# Purpose:   OCI image label generation for container builds
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""OCI-standard label generation for container image builds."""

from __future__ import annotations

from datetime import UTC, datetime


def build_oci_labels(
    *,
    repo: str,
    revision: str,
    version: str,
    title: str,
    description: str = "",
    extra_labels: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a dict of OCI-standard image labels.

    Args:
        repo: GitHub repository in ``owner/name`` form.
        revision: Git commit SHA or ref.
        version: Semantic version string.
        title: Human-readable image title.
        description: Optional image description.
        extra_labels: Additional labels merged into the result.

    Returns:
        Mapping of label keys to values.
    """
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    labels: dict[str, str] = {
        "org.opencontainers.image.source": f"https://github.com/{repo}",
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.created": created,
        "org.opencontainers.image.title": title,
        "org.opencontainers.image.description": description,
        "org.opencontainers.image.vendor": "HYPERI PTY LIMITED",
        "org.opencontainers.image.licenses": "FSL-1.1-ALv2",
        "io.hyperi.profile": "production",
    }

    if extra_labels:
        labels.update(extra_labels)

    return labels


def labels_to_build_args(labels: dict[str, str]) -> list[str]:
    """Convert a label dict to sorted ``--label key=value`` CLI argument pairs.

    Args:
        labels: Mapping of label keys to values.

    Returns:
        Flat list of alternating ``--label`` flags and ``key=value`` strings,
        sorted by key.
    """
    args: list[str] = []
    for key in sorted(labels):
        args.append("--label")
        args.append(f"{key}={labels[key]}")
    return args
