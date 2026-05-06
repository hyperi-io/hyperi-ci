# Project:   HyperI CI
# File:      src/hyperi_ci/publish/__init__.py
# Purpose:   Publish package — binaries + retroactive dispatch
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Publish package.

Two related modules:

- :mod:`hyperi_ci.publish.binaries` — language-agnostic binary
  publisher. Uploads pre-built artefacts from ``dist/`` to GitHub
  Release, R2 (Cloudflare), and JFrog (deprecated). Called from the
  publish stage handler in ``dispatch.py``.

- :mod:`hyperi_ci.publish.dispatch` — retroactive publish via
  workflow_dispatch on an existing tag. The primary publish path is
  ``hyperi-ci push --publish`` which goes through the version-first
  single-run pipeline; this module covers the "re-publish an existing
  tag" escape hatch.

The CLI ``hyperi-ci publish <tag>`` command (and its ``release`` alias
for back-compat) routes through :func:`dispatch_publish`.
"""

from hyperi_ci.publish.binaries import (
    create_github_release,
    publish_binaries,
)
from hyperi_ci.publish.dispatch import (
    dispatch_publish,
    list_unpublished,
    resolve_latest_tag,
)

__all__ = [
    "create_github_release",
    "dispatch_publish",
    "list_unpublished",
    "publish_binaries",
    "resolve_latest_tag",
]
