# Project:   HyperI CI
# File:      src/hyperi_ci/publish_mode.py
# Purpose:   Back-compat shim — moved to hyperi_ci.release_mode
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""DEPRECATED: this module moved to :mod:`hyperi_ci.release_mode`.

Importers should update to::

    from hyperi_ci.release_mode import resolve_push_mode, is_release_mode

Every name the old module exported is re-exported here, ``PUBLISH``
included -- it now carries the ``release`` token, which the mode
comparisons treat identically.
"""

from __future__ import annotations

import warnings

from hyperi_ci.release_mode import (  # noqa: F401
    DEV,
    PUBLISH,
    RELEASE,
    VALIDATE,
    dev_branch_slug,
    is_branch_ci_context,
    is_publish_mode,
    is_release_mode,
    resolve_push_mode,
)

warnings.warn(
    "hyperi_ci.publish_mode is deprecated; use hyperi_ci.release_mode",
    DeprecationWarning,
    stacklevel=2,
)
