# Project:   HyperI CI
# File:      src/hyperi_ci/container/labels.py
# Purpose:   OCI image label generation for container builds
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""OCI-standard label generation for container image builds."""

from datetime import UTC, datetime

from hyperi_ci.licenses import DEFAULT_LICENSE

_OCI_KEY_PREFIX = "org.opencontainers.image."

HYPERI_VENDOR = "HYPERI PTY LIMITED"

# Canonical classifications whose images HyperI owns, and so may name HyperI
# as vendor and fall back to BUSL-1.1.
HYPERI_OWNED: frozenset[str] = frozenset({"internal", "product"})


def ownership_labels(classification: str, licence: str | None) -> dict[str, str]:
    """Return the vendor and licence labels a repo's classification allows.

    A HyperI-owned repo names HyperI as vendor and falls back to BUSL-1.1. Any
    other repo, an undeclared one included, gets no vendor label and a licence
    label only when a licence was found, because a guessed IP statement on a
    published image is worse than none.

    Args:
        classification: Canonical declared classification, "" when undeclared.
        licence: SPDX id declared or detected for the repo, None when none was
            found.

    Returns:
        Zero to two labels: ``org.opencontainers.image.vendor`` and
        ``org.opencontainers.image.licenses``.

    """
    labels: dict[str, str] = {}
    if classification in HYPERI_OWNED:
        labels[f"{_OCI_KEY_PREFIX}vendor"] = HYPERI_VENDOR
        licence = licence or DEFAULT_LICENSE
    if licence:
        labels[f"{_OCI_KEY_PREFIX}licenses"] = licence
    return labels


def build_oci_labels(
    *,
    repo: str,
    revision: str,
    version: str,
    title: str,
    description: str = "",
    classification: str = "",
    licenses: str | None = None,
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
        classification: Canonical declared repo classification, "" when the
                        repo declares none. Decides the vendor label and the
                        licence fallback, see :func:`ownership_labels`.
        licenses: SPDX licence id declared or detected for the repo, None
                  when none was found.
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
        **ownership_labels(classification, licenses),
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
