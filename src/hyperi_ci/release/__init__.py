# Project:   HyperI CI
# File:      src/hyperi_ci/release/__init__.py
# Purpose:   Release package — binaries + retroactive dispatch
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release package.

Two related modules:

- :mod:`hyperi_ci.release.binaries` -- language-agnostic binary
  publisher. Uploads pre-built artefacts from ``dist/`` to GitHub
  Releases and Cloudflare R2 (``downloads.hyperi.io``). Called from
  the release stage handler in ``dispatch.py``.

- :mod:`hyperi_ci.release.dispatch` -- retroactive release via
  workflow_dispatch on an existing tag. The primary path is
  ``hyperi-ci push --release``, which goes through the version-first
  single-run pipeline; this module covers the "re-release an existing
  tag" escape hatch.

The CLI ``hyperi-ci release <tag>`` command, and the deprecated
``publish`` alias beside it, route through :func:`dispatch_publish`.
"""

from hyperi_ci.release.binaries import (
    create_github_release,
    publish_binaries,
)
from hyperi_ci.release.dispatch import (
    dispatch_from_head,
    dispatch_publish,
    list_unpublished,
    resolve_latest_tag,
)

__all__ = [
    "create_github_release",
    "dispatch_from_head",
    "dispatch_publish",
    "list_unpublished",
    "publish_binaries",
    "resolve_latest_tag",
]
