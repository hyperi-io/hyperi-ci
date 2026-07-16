# Project:   HyperI CI
# File:      src/hyperi_ci/quality/semgrep.py
# Purpose:   Semgrep SAST scanning (cross-language, dispatch-level)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Semgrep SAST scanning (cross-language).

Semgrep's auto ruleset spans languages (python, go, ts, rust, yaml,
Dockerfiles, ...), so it runs ONCE at the dispatch level - like
gitleaks - rather than being re-invoked inside each language handler.
Centralising it also fixes a drift: only the Python handler passed the
shared exclude-dirs to semgrep; go / ts / rust did not.

Mode comes from ``quality.semgrep`` (default ``warn``). A consumer's
legacy per-language ``quality.<lang>.semgrep`` override is still honoured
for back-compat. Path excludes come from the shared exclude-dirs; rule
suppressions from the ``quality.ignore`` list (``tool: semgrep``).
"""

from __future__ import annotations

import shutil
import subprocess

from hyperi_ci.common import error, get_exclude_dirs, info, is_ci, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import apply_strict, is_skipped
from hyperi_ci.quality.ignores import for_tool, load_ignores
from hyperi_ci.tools import missing_tool_notice


def _resolve_mode(config: CIConfig, language: str | None) -> str:
    """Resolve semgrep's mode, honouring a legacy per-language override.

    ``quality.semgrep`` is the current key. A consumer that still sets
    ``quality.<language>.semgrep`` (the pre-centralisation location) wins
    for back-compat - defaults.yaml no longer carries the per-language
    entries, so a per-language value can only come from the consumer.
    """
    if is_skipped("semgrep"):
        return "disabled"
    mode = str(config.get("quality.semgrep", "warn"))
    if language:
        legacy = config.get(f"quality.{language}.semgrep")
        if legacy is not None:
            mode = str(legacy)
    return apply_strict(mode)


def run(config: CIConfig, *, language: str | None = None) -> int:
    """Run semgrep SAST across the repo.

    Args:
        config: Merged CI configuration.
        language: Detected project language, used only for the legacy
            per-language mode override.

    Returns:
        Exit code (0 = success / non-blocking / skipped).

    """
    mode = _resolve_mode(config, language)
    if mode == "disabled":
        info("  semgrep: disabled")
        return 0

    if shutil.which("semgrep"):
        cmd = ["semgrep"]
    elif shutil.which("uvx"):
        cmd = ["uvx", "semgrep"]
    else:
        # Not installed and no uvx fallback: fail only in CI (where every
        # tool MUST be present - a silent skip masks a coverage gap);
        # warn-skip locally. Matches the gitleaks + language _run_tool
        # local-vs-CI handling.
        notice = missing_tool_notice("semgrep")
        if mode == "blocking" and is_ci():
            error(notice)
            return 1
        warn(notice)
        return 0

    cmd += ["scan", "--config", "auto", "--error", "--quiet"]
    for exc in get_exclude_dirs(config._raw):
        cmd.extend(["--exclude", exc])
    for entry in for_tool(load_ignores(config._raw), "semgrep"):
        cmd.extend(["--exclude-rule", entry.id])

    info("  semgrep: scanning for SAST findings...")
    result = subprocess.run(cmd)

    if result.returncode == 0:
        success("  semgrep: passed")
        return 0
    if mode == "warn":
        warn("  semgrep: findings (non-blocking)")
        return 0
    error("  semgrep: findings above must be fixed or ignored")
    return 1
