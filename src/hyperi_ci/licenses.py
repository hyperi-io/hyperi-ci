# Project:   HyperI CI
# File:      src/hyperi_ci/licenses.py
# Purpose:   Canonical licence registry and project-licence allow policy
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Canonical licence registry and project-licence allow policy.

hyperi-ci recognises the common SPDX licences (used for licence
detection and OCI image labelling) but only *allows* a small blessed
set by default - BUSL-1.1, Apache-2.0 and MIT. A project declaring any
other recognised licence gets a non-blocking warning telling it to opt
in via ``license_allow`` in ``.hyperi-ci.yaml``; an unrecognised id is
flagged as a likely typo.
"""

from __future__ import annotations

from collections.abc import Iterable

# The blessed default set. A project may declare any of these as its
# ``license:`` without further config.
DEFAULT_ALLOWED: tuple[str, ...] = ("BUSL-1.1", "Apache-2.0", "MIT")

# Fallback used when a project's licence cannot be resolved any other way.
DEFAULT_LICENSE: str = "BUSL-1.1"

# Substring markers used to identify a licence from LICENSE-file text or a
# source-file header when the project does not declare ``license:``
# explicitly. Limited to ids with distinctive, unambiguous wording - the
# GPL/LGPL/BSD families share enough boilerplate that text sniffing is
# unreliable, so those are recognised (below) but must be declared
# explicitly rather than guessed.
LICENSE_MARKERS: dict[str, tuple[str, ...]] = {
    "BUSL-1.1": ("BUSL-1.1", "Business Source License"),
    "Apache-2.0": ("Apache License", "Licensed under the Apache"),
    "MIT": ("MIT License", "Permission is hereby granted, free of charge"),
    "MPL-2.0": ("Mozilla Public License",),
    "ISC": ("ISC License",),
    "BSL-1.0": ("Boost Software License",),
    "Unlicense": ("This is free and unencumbered software", "Unlicense"),
    "CC0-1.0": ("CC0 1.0 Universal", "Creative Commons Zero"),
    "Zlib": ("zlib License",),
    "AGPL-3.0": ("GNU AFFERO GENERAL PUBLIC LICENSE",),
}

# Every SPDX id hyperi-ci recognises for policy purposes - a superset of
# LICENSE_MARKERS, including ids that must be declared rather than sniffed.
RECOGNISED: frozenset[str] = frozenset(
    {
        *LICENSE_MARKERS,
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-2.0",
        "GPL-3.0",
        "LGPL-2.1",
        "LGPL-3.0",
        "EPL-2.0",
        "Artistic-2.0",
        "Unicode-3.0",
    }
)


def allowed_licenses(extra: Iterable[str] | None = None) -> set[str]:
    """Return the set of allowed project licences.

    Args:
        extra: Additional SPDX ids to allow beyond the default set,
            typically from ``license_allow`` in ``.hyperi-ci.yaml``.

    Returns:
        The union of the default-allowed ids and any extras.

    """
    allowed = set(DEFAULT_ALLOWED)
    if extra:
        allowed.update(e.strip() for e in extra if isinstance(e, str) and e.strip())
    return allowed


def is_recognised(license_id: str) -> bool:
    """Return whether ``license_id`` is a known SPDX id.

    Args:
        license_id: The SPDX identifier to check.

    Returns:
        True if the id is recognised by hyperi-ci.

    """
    return license_id in RECOGNISED


def is_allowed(license_id: str, extra: Iterable[str] | None = None) -> bool:
    """Return whether ``license_id`` is permitted as a project licence.

    Args:
        license_id: The SPDX identifier to check.
        extra: Additional allowed ids (e.g. from ``license_allow``).

    Returns:
        True if the licence is in the allowed set.

    """
    return license_id in allowed_licenses(extra)
