#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/tag_version.py
# Purpose:   Write the version a re-published tag names to the step outputs
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Write ``version`` to ``$GITHUB_OUTPUT`` for a retroactive ``tag`` dispatch.

The version is the tag's own, never the tagged tree's: that tree carries the
release before it (issue #352). ``tag_version`` in
``src/hyperi_ci/version_source.py`` decides; the composite runs where
hyperi-ci is not installed, so it is loaded out of the action's own checkout,
the same by-path approach ``resolve_tier.py`` takes.

The tag arrives as ``RELEASE_TAG``, never interpolated into a script. A tag
that names no version fails the step: publishing under a guessed version is
the #105 failure.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs this composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[3] / "src" / "hyperi_ci"


def run() -> int:
    """Resolve the tag's version and write it as the ``version`` output."""
    # hyperi_ci/__init__ reads installed package metadata, which is absent here.
    if "hyperi_ci" not in sys.modules:
        package = types.ModuleType("hyperi_ci")
        package.__path__ = [str(_PACKAGE)]
        sys.modules["hyperi_ci"] = package
    from hyperi_ci.version_source import tag_version

    tag = os.environ.get("RELEASE_TAG", "")
    version = tag_version(tag)
    if version is None:
        print(
            f"::error title=tag dispatch::'{tag}' is not a release tag (vX.Y.Z or "
            "vX.Y.Z-pre) -- refusing to re-publish under a version it does not name"
        )
        return 1

    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"version={version}\n")
    else:
        sys.stdout.write(f"version={version}\n")
    print(f"::notice title=tag dispatch::Re-publishing {tag} as version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
