# Project:   HyperI CI
# File:      src/hyperi_ci/publish_binaries.py
# Purpose:   Back-compat shim — moved to hyperi_ci.publish.binaries
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""DEPRECATED: this module moved to :mod:`hyperi_ci.publish.binaries`.

Importers should update to::

    from hyperi_ci.publish import publish_binaries, create_github_release

This shim is kept for one release cycle to avoid breaking out-of-tree
callers. Will be removed in v3.0.
"""

from __future__ import annotations

import warnings

warnings.warn(
    "hyperi_ci.publish_binaries is deprecated; use hyperi_ci.publish "
    "(or hyperi_ci.publish.binaries for the binary helpers)",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export the public API + the private helpers tests reach into
from hyperi_ci.publish.binaries import (  # noqa: E402,F401
    _resolve_gh_release_flags,
    _resolve_r2_paths,
    create_github_release,
    publish_binaries,
)
