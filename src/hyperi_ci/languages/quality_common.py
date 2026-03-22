# Project:   HyperI CI
# File:      src/hyperi_ci/languages/quality_common.py
# Purpose:   Shared utilities for two-tier quality (production/test) rule splitting
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared quality check utilities for two-tier (production/test) rule splitting.

Quality checks run in two passes:
1. Production pass — full strict rules on all code except test dirs
2. Test pass — relaxed rules on test directories only

Test paths and ignore lists are configurable via defaults.yaml and
overridable per project in .hyperi-ci.yaml.
"""

from __future__ import annotations

from pathlib import Path

from hyperi_ci.config import CIConfig

DEFAULT_TEST_PATHS = ["tests/"]


def get_test_paths(config: CIConfig) -> list[str]:
    """Get configured test directories that exist on disk.

    Reads quality.test_paths from config, defaults to ["tests/"].
    Only returns paths that actually exist as directories.
    """
    configured = config.get("quality.test_paths", DEFAULT_TEST_PATHS)
    if not isinstance(configured, list):
        configured = DEFAULT_TEST_PATHS
    return [p for p in configured if Path(p).is_dir()]


def get_test_ignore(language: str, config: CIConfig, defaults: list[str]) -> list[str]:
    """Get test_ignore rules for a language, with fallback to defaults.

    Projects override entirely via quality.<language>.test_ignore
    in .hyperi-ci.yaml. If not set, uses the provided defaults
    (which come from defaults.yaml).
    """
    configured = config.get(f"quality.{language}.test_ignore", None)
    if configured is not None and isinstance(configured, list):
        return [str(r) for r in configured]
    return defaults
