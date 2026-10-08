# Project:   HyperI CI
# File:      src/hyperi_ci/quality/semgrep.py
# Purpose:   Semgrep SAST scanning (cross-language, dispatch-level)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Semgrep SAST scanning, once at dispatch level because its ruleset spans languages.

Mode is ``quality.semgrep`` (default ``warn``), else a legacy
``quality.<lang>.semgrep``. Excludes are the shared exclude-dirs, the
``quality.ignore`` entries for ``semgrep``, and the
``python.lang.compatibility.*`` rules the ``requires-python`` floor has outgrown.
"""

import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, get_exclude_dirs, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import (
    apply_strict,
    checked_mode,
    is_skipped,
    note_gate_downgrade,
    resolve_tool_cmd,
)
from hyperi_ci.python_version import requires_python_floor
from hyperi_ci.quality.ignores import for_tool, load_ignores
from hyperi_ci.tools import missing_tool
from hyperi_ci.versions import tool_version

_SHIPPED_KEY = "quality.semgrep"

# The `r/python.lang.compatibility` pack at semgrep 1.178.0, rule id to target
# Python. The registry serves it at scan time, so
# scripts/check-semgrep-compat-rules.py re-checks it weekly.
PYTHON_COMPAT_RULES: dict[str, str] = {
    "python.lang.compatibility.python36.python36-compatibility-Popen1": "3.6",
    "python.lang.compatibility.python36.python36-compatibility-Popen2": "3.6",
    "python.lang.compatibility.python36.python36-compatibility-ssl": "3.6",
    "python.lang.compatibility.python37.python37-compatibility-httpconn": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-httpsconn": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-importlib": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-importlib2": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-importlib3": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-ipv4network1": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-ipv4network2": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-ipv6network1": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-ipv6network2": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-locale1": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-math1": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-multiprocess1": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-multiprocess2": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-os1": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-os2-ok2": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-pdb": "3.7",
    "python.lang.compatibility.python37.python37-compatibility-textiowrapper": "3.7",
}


def _stale_compat_rules(floor: str | None) -> list[str]:
    """Return the compatibility rule ids a ``requires-python`` floor makes moot.

    A ``None`` floor excludes nothing.
    """
    if not floor:
        return []
    floor_tuple = tuple(int(p) for p in floor.split("."))
    return [
        rule_id
        for rule_id, target in PYTHON_COMPAT_RULES.items()
        if floor_tuple >= tuple(int(p) for p in target.split("."))
    ]


def _resolve_mode(config: CIConfig, language: str | None) -> str:
    """Resolve semgrep's mode, with a legacy ``quality.<language>.semgrep`` winning.

    Raises:
        GateReasonRequiredError: The gate is turned below the shipped default
            with no reason beside it.

    """
    if is_skipped("semgrep"):
        return "disabled"
    key = _SHIPPED_KEY
    raw = config.get(key, "warn")
    if language and (legacy := config.get(f"quality.{language}.semgrep")) is not None:
        key = f"quality.{language}.semgrep"
        raw = legacy
    mode, reason = checked_mode(key, raw, "warn")
    # The legacy key has no shipped default, so both are measured against semgrep's.
    note_gate_downgrade(key, mode, reason, shipped_key=_SHIPPED_KEY)
    return apply_strict(mode)


def run(config: CIConfig, *, language: str | None = None) -> int:
    """Run semgrep SAST across the repo.

    Args:
        config: Merged CI configuration.
        language: Detected project language, used only for the legacy
            per-language mode override.

    Returns:
        Exit code (0 = success / non-blocking / skipped).

    Raises:
        GateReasonRequiredError: The gate is turned below the shipped default
            with no reason beside it.

    """
    mode = _resolve_mode(config, language)
    if mode == "disabled":
        info("  semgrep: disabled")
        return 0

    # The pin wins over a semgrep on PATH whenever uv can install it.
    spec = f"semgrep=={tool_version('semgrep')}"
    cmd = resolve_tool_cmd(["semgrep"], via="uvx", spec=spec)
    if cmd == ["semgrep"] and not shutil.which("semgrep"):
        return missing_tool("semgrep", mode)

    cmd += ["scan", "--config", "auto", "--error", "--quiet"]
    for exc in get_exclude_dirs(config._raw):
        cmd.extend(["--exclude", exc])
    for entry in for_tool(load_ignores(config._raw), "semgrep"):
        cmd.extend(["--exclude-rule", entry.id])
    for rule_id in _stale_compat_rules(requires_python_floor(Path.cwd())):
        cmd.extend(["--exclude-rule", rule_id])

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
