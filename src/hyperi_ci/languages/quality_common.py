# Project:   HyperI CI
# File:      src/hyperi_ci/languages/quality_common.py
# Purpose:   Shared utilities for two-tier quality (production/test) rule splitting
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared quality check utilities for two-tier (production/test) rule splitting.

Quality checks run in two passes:
1. Production pass — full strict rules on all code except test dirs
2. Test pass — relaxed rules on test directories only

Test paths and ignore lists are configurable via defaults.yaml and
overridable per project in .hyperi-ci.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path

from hyperi_ci.common import is_ci, warn
from hyperi_ci.config import CIConfig

DEFAULT_TEST_PATHS = ["tests/"]

_STRICT_TRUTHY = {"1", "true", "yes", "on"}


def strict_quality() -> bool:
    """Return True when strict quality mode is active.

    Strict mode upgrades ``warn``-tier findings to ``blocking`` so a
    developer sees - and then fixes or explicitly ignores - everything
    CI would surface BEFORE the push, not after. Enabled by
    ``hyperi-ci check --strict`` (which exports ``HYPERCI_QUALITY_STRICT``)
    or by exporting that env var directly.
    """
    return (
        os.environ.get("HYPERCI_QUALITY_STRICT", "").strip().lower() in _STRICT_TRUTHY
    )


def apply_strict(mode: str) -> str:
    """Upgrade a ``warn`` mode to ``blocking`` under :func:`strict_quality`.

    Shared by :func:`resolve_tool_mode` (per-language tools) and the
    dispatch-level cross-language scans (semgrep) so strict behaves
    identically whichever layer resolved the mode. ``disabled`` and
    ``blocking`` pass through unchanged.
    """
    if mode == "warn" and strict_quality():
        return "blocking"
    return mode


def quality_skip() -> frozenset[str]:
    """Tool names to forcibly skip this run (``HYPERCI_QUALITY_SKIP``).

    RARE edge-case escape hatch. When a tool's false positive halts CI
    - a semgrep rule misfiring on a dependency, an audit advisory with
    no fix yet - set ``HYPERCI_QUALITY_SKIP=semgrep`` (comma-separated
    for several) on the blocked runs to skip that tool WITHOUT a config
    commit, then remove it once the real fix (a rule ignore / version
    bump) lands. This is deliberately an env override, not a config knob:
    the reviewed config path (``quality.<tool>: disabled`` or the
    ``quality.ignore`` list) stays the normal way to silence a tool. A
    force-skip is logged loudly (:func:`is_skipped`).
    """
    raw = os.environ.get("HYPERCI_QUALITY_SKIP", "")
    return frozenset(t.strip().lower() for t in raw.split(",") if t.strip())


def is_skipped(tool: str) -> bool:
    """Return True if ``tool`` is force-skipped, surfacing it loudly.

    A force-skip is an emergency override that must NOT pass unnoticed -
    especially for a security scanner like gitleaks. In CI it emits a
    real GitHub ``::warning::`` annotation (which escapes the collapsed
    log group and lands in the run summary), not just a logger line that
    hides inside a folded group.
    """
    if tool.lower() not in quality_skip():
        return False
    msg = (
        f"{tool}: FORCE-SKIPPED via HYPERCI_QUALITY_SKIP - rare edge-case "
        f"override; remove it once the false positive is fixed"
    )
    warn(f"  {msg}")
    if is_ci():
        print(f"::warning title=hyperi-ci quality force-skip::{msg}")
    return True


def resolve_tool_mode(tool: str, config: CIConfig, language: str) -> str:
    """Resolve a quality tool's mode: ``blocking``, ``warn`` or ``disabled``.

    Reads ``quality.<language>.<tool>`` from config (default
    ``blocking``). A force-skip (:func:`is_skipped`) wins - the tool is
    ``disabled`` for this run. Otherwise, under strict mode
    (:func:`strict_quality`) a ``warn`` tool is upgraded to ``blocking``;
    ``disabled`` is left untouched.
    """
    if is_skipped(tool):
        return "disabled"
    return apply_strict(str(config.get(f"quality.{language}.{tool}", "blocking")))


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
