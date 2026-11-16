# Project:   HyperI CI
# File:      src/hyperi_ci/release/semantic_release.py
# Purpose:   The pinned npm set behind the semantic-release tagger
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The npm packages setup-semantic-release installs, versions from versions.yaml.

The action runs ``npm ci`` against the package.json and package-lock.json beside
it, so the whole tree is locked, not only the direct pins.
``scripts/relock-node-tools.py`` writes both from :func:`manifest`.
"""

from hyperi_ci.versions import tool_npm, tool_version

TOOLS = (
    "semantic-release",
    "semantic-release-commit-analyzer",
    "semantic-release-release-notes-generator",
    "semantic-release-changelog",
    "semantic-release-exec",
    "semantic-release-github",
    "conventional-changelog-conventionalcommits",
)

# Matches the `name` in the lockfile, which npm writes from this manifest.
MANIFEST_NAME = "hyperi-ci-semantic-release"


def manifest() -> dict[str, object]:
    """Return the ``package.json`` for the pinned set, versions from the SSOT."""
    return {
        "name": MANIFEST_NAME,
        "private": True,
        "dependencies": {tool_npm(name): tool_version(name) for name in TOOLS},
    }
