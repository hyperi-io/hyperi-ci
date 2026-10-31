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
import platform

#: Variables a CI runner sets. Any one non-empty means CI.
CI_ENV_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE")

_MACHINE_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def is_ci() -> bool:
    """Return True when running on a CI runner."""
    return any(os.environ.get(v) for v in CI_ENV_VARS)


def machine_arch(machine: str | None = None) -> str | None:
    """Return ``amd64`` or ``arm64`` for this host's CPU, None for any other."""
    return _MACHINE_ARCH.get((machine or platform.machine()).lower())
