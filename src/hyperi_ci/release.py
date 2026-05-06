# Project:   HyperI CI
# File:      src/hyperi_ci/release.py
# Purpose:   Back-compat shim — moved to hyperi_ci.publish.dispatch
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""DEPRECATED: this module moved to :mod:`hyperi_ci.publish.dispatch`.

Importers should update to::

    from hyperi_ci.publish import dispatch_publish, list_unpublished

This shim is kept for one release cycle to avoid breaking out-of-tree
callers. Will be removed in v3.0.
"""

from __future__ import annotations

import warnings

warnings.warn(
    "hyperi_ci.release is deprecated; use hyperi_ci.publish "
    "(or hyperi_ci.publish.dispatch for the dispatch helpers)",
    DeprecationWarning,
    stacklevel=2,
)

from hyperi_ci.publish.dispatch import (  # noqa: E402,F401
    dispatch_publish,
    list_unpublished,
    resolve_latest_tag,
)
