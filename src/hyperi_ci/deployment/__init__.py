# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/__init__.py
# Purpose:   Deployment-artefact producer detection
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Detect whether a repo produces its own deployment artefacts.

A scalo app emits its Dockerfile, chart and contract itself through
``<app> generate-artefacts``. This package only reads manifests to say
whether a repo is such a producer, and which binary or entry point it runs.
"""

from hyperi_ci.deployment.detect import Tier, TierDecision, detect_tier, resolve_tier

__all__ = ["Tier", "TierDecision", "detect_tier", "resolve_tier"]
