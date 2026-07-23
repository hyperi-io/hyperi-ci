# Project:   HyperI CI
# File:      src/hyperi_ci/quality/hadolint.py
# Purpose:   hadolint Dockerfile linting (cross-language gate, dispatch-level)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""hadolint Dockerfile linting - the container GATE.

hadolint is the one Dockerfile linter allowed to FAIL a build, because it
lints the shell inside ``RUN`` instructions via ShellCheck - a correctness
check nothing else in this space offers. Runs once at the dispatch level (like
gitleaks/semgrep) over every Dockerfile in the repo; auto-detects and clean-
skips a repo with none.

**Gate semantics.** hadolint's own rules carry severities (error / warning /
info / style). We gate on ERROR severity only: in ``blocking`` mode an
error-level finding fails the stage, while warning/info/style are surfaced but
never fatal. That is deliberate - the estate's routine noise (DL3008 apt-pin,
DL4006 pipefail) is all warning-tier, so the gate is near-silent day one while
still catching a broken ``RUN`` shell. ``warn`` mode never fails; ``--strict``
upgrades ``warn`` to ``blocking``.

Findings surface through the shared layer (:mod:`hyperi_ci.quality.findings`):
bounded annotations + full job-summary table + optional SARIF.
"""

from __future__ import annotations

import json
import platform
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
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.install import install_ci_binary
from hyperi_ci.quality.targets import discover_dockerfiles
from hyperi_ci.tools import missing_tool_notice

# Mirrors `tools.hadolint` in config/versions.yaml (the SSoT). config/ ships
# outside the wheel, so the value is copied here; update-versions.py --fix keeps
# them in sync via the marker below. Do not hand-edit.
# hyperi-ci:pin tools.hadolint
_HADOLINT_VERSION = "v2.14.0"

# sha256 pinned from hadolint v2.14.0 Linux release, verified before exec.
# Keyed by the arch token in the asset name; the raw binary is hashed as-is.
_HADOLINT_SHA256 = {
    "x86_64": "6bf226944684f56c84dd014e8b979d27425c0148f61b3bd99bcc6f39e9dc5a47",
    "arm64": "331f1d3511b84a4f1e3d18d52fec284723e4019552f4f47b19322a53ce9a40ed",
}


def _install_hadolint() -> str | None:
    """Return a hadolint path, installing the pinned static binary on Linux CI.

    hadolint is baked into the ARC runner image, but consumer CI can run on a
    vanilla GitHub runner where it is absent. It is a single static binary, so
    fetch the pinned release rather than let a blocking gate hard-fail for a
    missing tool. Returns None off-CI / non-Linux (the caller warn-skips locally).
    """
    arch = "x86_64" if platform.machine() in ("x86_64", "AMD64") else "arm64"
    url = (
        f"https://github.com/hadolint/hadolint/releases/download/"
        f"{_HADOLINT_VERSION}/hadolint-Linux-{arch}"
    )
    return install_ci_binary("hadolint", url, expected_sha256=_HADOLINT_SHA256[arch])


def _rule_url(code: str) -> str:
    """Docs link for a hadolint (DL*) or embedded ShellCheck (SC*) rule."""
    if code.startswith("DL"):
        return f"https://github.com/hadolint/hadolint/wiki/{code}"
    if code.startswith("SC"):
        return f"https://www.shellcheck.net/wiki/{code}"
    return ""


def _parse(stdout: str) -> list[fdg.Finding]:
    """Parse hadolint ``--format json`` output into normalised findings.

    hadolint emits a flat array of ``{file, line, column, level, code,
    message}``. A blank / non-array payload yields ``[]`` (no findings).
    """
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


def _resolve_mode(config: CIConfig) -> str:
    """Resolve hadolint's mode: ``blocking`` (default) / ``warn`` / ``disabled``."""
    return resolve_cross_tool_mode(config, "hadolint", "blocking")


def run(config: CIConfig, *, sarif_path: str | Path | None = None) -> int:
    """Run hadolint over every Dockerfile in the repo.

    Returns exit code (0 = pass / advisory / skipped; 1 = a blocking gate hit an
    error-severity finding, or the tool is required-but-missing in CI).
    """
    mode = _resolve_mode(config)
    if mode == "disabled":
        info("  hadolint: disabled")
        return 0

    dockerfiles = discover_dockerfiles(
        Path.cwd(), exclude_dirs=get_exclude_dirs(config._raw)
    )
    if not dockerfiles:
        info("  hadolint: no Dockerfile found - skipping")
        return 0

    exe = _install_hadolint()
    if not exe:
        if mode == "blocking" and is_ci():
            error(missing_tool_notice("hadolint"))
            return 1
        warn(missing_tool_notice("hadolint"))
        return 0

    # --no-fail: hadolint always exits 0 and emits the full JSON report; WE
    # decide the gate from the parsed severities, so all gate logic is in one
    # place (and testable) rather than split across hadolint's exit code.
    rels = [str(p.relative_to(Path.cwd())) for p in dockerfiles]
    info(f"  hadolint: linting {len(rels)} Dockerfile(s)...")
    try:
        result = run_cmd(
            [exe, "--no-fail", "--format", "json", *rels], check=False, capture=True
        )
    except OSError as exc:
        # exec failure (a corrupt auto-installed binary, no exec bit). A gate
        # that cannot run is not a pass - fail it in CI rather than crash.
        warn(f"  hadolint could not be run ({exc})")
        if mode == "blocking" and is_ci():
            error("  hadolint could not complete - failing the gate")
            return 1
        return 0
    found = _parse(result.stdout)

    # --no-fail means hadolint exits 0 even WITH findings, so a non-zero exit
    # with nothing parsed is the TOOL erroring (a drifted flag, a corrupt
    # download that still chmod'd, OOM) - NOT a clean Dockerfile. A gate that
    # scores a malfunctioning tool as green is the worst failure mode, so treat
    # it as unvalidated: blocking-in-CI fails, otherwise warn and carry on.
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
