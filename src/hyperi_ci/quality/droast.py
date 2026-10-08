# Project:   HyperI CI
# File:      src/hyperi_ci/quality/droast.py
# Purpose:   droast Dockerfile linting (cross-language ADVISORY, dispatch-level)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""droast Dockerfile linting; advisory, never fails a build.

droast (immanuwell/dockerfile-roast) covers what hadolint has no rule for:
broad ``COPY`` before install (DF070), ``.dockerignore`` effectiveness (DF033)
and ``npm install`` where ``npm ci`` belongs (DF031). It stays advisory in every
mode because the project is young (first published 2026-07-18, one
maintainer). Its SARIF output goes through the shared parser.
"""

from pathlib import Path

from hyperi_ci.common import get_exclude_dirs, info, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.targets import discover_dockerfiles
from hyperi_ci.tools import find_tool

# Shipped default: skip DF007/DF012, min-severity=info, no-roast.
_DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[1] / "config" / "droast" / "droast.toml"
)


def run(config: CIConfig, *, sarif_path: str | Path | None = None) -> int:
    """Run droast over every Dockerfile; always returns 0."""
    if resolve_tool_mode("droast", config, default="warn") == "disabled":
        info("  droast: disabled")
        return 0

    dockerfiles = discover_dockerfiles(
        Path.cwd(), exclude_dirs=get_exclude_dirs(config._raw)
    )
    if not dockerfiles:
        info("  droast: no Dockerfile found - skipping")
        return 0

    exe = find_tool("droast", recommended=False)
    if not exe:
        return 0  # advisory: a missing droast info-skips, never fails

    # A droast.toml in the working directory is left for droast to discover.
    cmd = [exe, "--no-fail", "--format", "sarif"]
    if not (Path.cwd() / "droast.toml").exists():
        cmd += ["--config", str(_DEFAULT_CONFIG)]
    cmd += [str(p.relative_to(Path.cwd())) for p in dockerfiles]

    info(f"  droast: advising on {len(dockerfiles)} Dockerfile(s)...")
    try:
        result = run_cmd(cmd, check=False, capture=True)
    except OSError as exc:
        warn(f"  droast could not be run ({exc}) - advisory only, not failing.")
        return 0

    found = fdg.parse_sarif(result.stdout, "droast")
    dropped = fdg.surface("droast", found, sarif_path=sarif_path)
    if found:
        warn(f"  droast: {len(found)} advisory finding(s)")
        if dropped:
            info(f"  droast: +{dropped} more in the job summary")
    return 0
