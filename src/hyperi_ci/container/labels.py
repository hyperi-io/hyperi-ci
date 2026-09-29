# Project:   HyperI CI
# File:      src/hyperi_ci/container/labels.py
# Purpose:   OCI image label generation for container builds
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""OCI-standard label generation for container image builds."""

from datetime import UTC, datetime

_OCI_KEY_PREFIX = "org.opencontainers.image."


def build_oci_labels(
    *,
    repo: str,
    revision: str,
    version: str,
    title: str,
    description: str = "",
    licenses: str = "BUSL-1.1",
    optimized: bool = True,
    extra_labels: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a dict of OCI-standard image labels.

    Args:
        repo: GitHub repository in ``owner/name`` form.
        revision: Git commit SHA or ref.
        version: Semantic version string.
        title: Human-readable image title.
        description: Optional image description.
        licenses: SPDX licence id for the image (defaults to BUSL-1.1).
        optimized: Whether the build ran its optimisation stage. False stamps
                   ``io.hyperi.optimized=false``, so an image cut for a fast
                   deploy-and-test loop can be told apart from a full release
                   without benchmarking it.
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
        "org.opencontainers.image.licenses": licenses,
        "io.hyperi.profile": "production",
        "io.hyperi.optimized": "true" if optimized else "false",
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


def labels_to_index_annotation_args(labels: dict[str, str]) -> list[str]:
    """Mirror the non-empty ``org.opencontainers.image.*`` labels onto the index.

    GHCR reads a multi-arch package's description from the image index
    annotations, not from the per-arch image labels.

    Args:
        labels: Mapping of label keys to values.

    Returns:
        Flat list of alternating ``--annotation`` flags and
        ``index:key=value`` strings, sorted by key.

    """
    args: list[str] = []
    for key in sorted(labels):
        if key.startswith(_OCI_KEY_PREFIX) and labels[key]:
            args.append("--annotation")
            args.append(f"index:{key}={labels[key]}")
    return args
