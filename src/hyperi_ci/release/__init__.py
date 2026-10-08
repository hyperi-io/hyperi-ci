# Project:   HyperI CI
# File:      src/hyperi_ci/release/__init__.py
# Purpose:   Release package -- binaries + retroactive dispatch
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release package.

- :mod:`hyperi_ci.release.binaries` -- uploads ``dist/`` artefacts to GitHub
  Releases and Cloudflare R2.
- :mod:`hyperi_ci.release.dispatch` -- releases HEAD or re-releases an
  existing tag via workflow_dispatch (``hyperi-ci release``).
- :mod:`hyperi_ci.release.charts` -- packages committed Helm charts and
  pushes them to an OCI registry (``hyperi-ci publish-charts``).
- :mod:`hyperi_ci.release.assemble` -- writes a thin chart on the
  scalo-service library from a deployment contract
  (``hyperi-ci chart assemble``).
"""

from hyperi_ci.release.binaries import (
    create_github_release,
    publish_binaries,
    stage_release_assets,
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
    "stage_release_assets",
]
