# Project:   HyperI CI
# File:      src/hyperi_ci/publish.py
# Purpose:   Back-compat shim — the package moved to hyperi_ci.release
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""DEPRECATED: this package moved to :mod:`hyperi_ci.release`.

Importers should update to::

    from hyperi_ci.release import create_github_release, publish_binaries

The submodules are registered under the old dotted path as well, so
``from hyperi_ci.publish.binaries import ...`` still resolves. A plain
module shim would break that spelling, and it is the one out-of-tree
callers are most likely to have written.
"""

from __future__ import annotations

import sys
import warnings

from hyperi_ci.release import binaries as _binaries
from hyperi_ci.release import (  # noqa: F401
    create_github_release,
    dispatch_from_head,
    dispatch_publish,
    list_unpublished,
    publish_binaries,
    resolve_latest_tag,
)
from hyperi_ci.release import dispatch as _dispatch

warnings.warn(
    "hyperi_ci.publish is deprecated; use hyperi_ci.release",
    DeprecationWarning,
    stacklevel=2,
)

sys.modules[f"{__name__}.binaries"] = _binaries
sys.modules[f"{__name__}.dispatch"] = _dispatch
