# Project:   HyperI CI
# File:      src/hyperi_ci/quality/hadolint.py
# Purpose:   hadolint Dockerfile linting (cross-language gate, dispatch-level)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""hadolint Dockerfile linting, the one Dockerfile gate.

It is the gate because it runs ShellCheck over ``RUN`` instructions. It runs
once at dispatch level over every Dockerfile, and skips a repo with none. Only
error-severity findings fail ``blocking`` mode: the routine warning-tier noise
(DL3008 apt pins, DL4006 pipefail) is surfaced but never fatal.
"""

import json
import subprocess
from pathlib import Path

from hyperi_ci.common import (
    error,
    get_exclude_dirs,
    info,
    is_ci,
    run_cmd,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.native_tools import ci_binary
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.targets import discover_dockerfiles
from hyperi_ci.tools import missing_tool_notice


def _rule_url(code: str) -> str:
    """Return the docs link for a hadolint (DL*) or ShellCheck (SC*) rule."""
    if code.startswith("DL"):
        return f"https://github.com/hadolint/hadolint/wiki/{code}"
    if code.startswith("SC"):
        return f"https://www.shellcheck.net/wiki/{code}"
    return ""


def _parse(stdout: str) -> list[fdg.Finding]:
    """Parse hadolint's ``--format json`` array into findings; ``[]`` if malformed."""
    try:
        raw = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[fdg.Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", ""))
        out.append(
            fdg.Finding(
                tool="hadolint",
                path=str(item.get("file", "")),
                line=item.get("line"),
                level=fdg.normalise_level(str(item.get("level", "warning"))),
                rule=code,
                message=str(item.get("message", "")),
                url=_rule_url(code),
            )
        )
    return out


def run(
    config: CIConfig,
    *,
    sarif_path: str | Path | None = None,
    root: Path | None = None,
    timeout: float | None = None,
    exclude_dirs: list[str] | None = None,
) -> int:
    """Run hadolint over every Dockerfile under ``root`` (default: the cwd).

    ``exclude_dirs`` replaces the configured exclude list when given.

    Returns 1 when a blocking gate hits an error-severity finding or a timeout,
    or in CI cannot run or complete; else 0.
    """
    mode = resolve_tool_mode("hadolint", config, default="blocking")
    if mode == "disabled":
        info("  hadolint: disabled")
        return 0

    base = (root or Path.cwd()).resolve()
    if exclude_dirs is None:
        exclude_dirs = get_exclude_dirs(config._raw)
    dockerfiles = discover_dockerfiles(base, exclude_dirs=exclude_dirs)
    if not dockerfiles:
        info("  hadolint: no Dockerfile found - skipping")
        return 0

    exe = ci_binary("hadolint")
    if not exe:
        if mode == "blocking" and is_ci():
            error(missing_tool_notice("hadolint"))
            return 1
        warn(missing_tool_notice("hadolint"))
        return 0

    # --no-fail: the gate is decided from the parsed severities, not the exit code.
    rels = [str(p.relative_to(base)) for p in dockerfiles]
    info(f"  hadolint: linting {len(rels)} Dockerfile(s)...")
    try:
        result = run_cmd(
            [exe, "--no-fail", "--format", "json", *rels],
            check=False,
            capture=True,
            cwd=base,
            timeout=timeout,
            own_group=True,
        )
    except subprocess.TimeoutExpired:
        message = f"  hadolint: no result within {timeout}s"
        if mode == "blocking":
            error(f"{message} - failing the gate")
            return 1
        warn(message)
        return 0
    except OSError as exc:
        warn(f"  hadolint could not be run ({exc})")
        if mode == "blocking" and is_ci():
            error("  hadolint could not complete - failing the gate")
            return 1
        return 0
    found = _parse(result.stdout)

    # Under --no-fail a failing exit with nothing parsed is the tool erroring.
    if result.returncode != 0 and not found:
        warn(
            f"  hadolint exited {result.returncode} with no parseable output - tool error, not a clean pass"
        )
        if mode == "blocking" and is_ci():
            error("  hadolint could not complete - failing the gate")
            return 1
        return 0

    dropped = fdg.surface("hadolint", found, sarif_path=sarif_path)
    if dropped:
        info(f"  hadolint: +{dropped} more finding(s) in the job summary")

    errors = [f for f in found if f.level == "error"]
    if not found:
        success("  hadolint: passed")
        return 0
    if mode == "blocking" and errors:
        error(f"  hadolint: {len(errors)} error-severity finding(s) must be fixed")
        return 1
    warn(f"  hadolint: {len(found)} finding(s) (non-blocking)")
    return 0
