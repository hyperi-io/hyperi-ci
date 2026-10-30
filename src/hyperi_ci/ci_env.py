# Project:   HyperI CI
# File:      src/hyperi_ci/ci_env.py
# Purpose:   Detect a CI runner from its environment, stdlib only
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Detect a CI runner from its environment.

Stdlib-only so the pytest plugin can import it without loading scalo.
"""

import os

#: Variables a CI runner sets. Any one non-empty means CI.
CI_ENV_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE")


def is_ci() -> bool:
    """Return True when running on a CI runner."""
    return any(os.environ.get(v) for v in CI_ENV_VARS)
