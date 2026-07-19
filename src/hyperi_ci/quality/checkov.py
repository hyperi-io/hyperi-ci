# Project:   HyperI CI
# File:      src/hyperi_ci/quality/checkov.py
# Purpose:   Checkov IaC security scanning (ADVISORY, Path B)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Checkov IaC security scanning - the k8s + IaC security ADVISORY.

Checkov is the security/misconfiguration layer: it reads Kubernetes manifests,
Helm charts, Kustomize AND Terraform/OpenTofu, auto-templating Helm/Kustomize
itself (no pre-render needed). One tool covers dfe-infra's charts and its `.tf`
- which is why it was chosen over Kubescape (Kubescape cannot scan OpenTofu).

Installed the same way as semgrep - `uvx checkov`, since uv is already a hard
dependency of every hyperi-ci project (no new tool manager).

**Advisory by default** (`warn`): its 1000+ policies are broad, and retrofitting
them onto an existing estate as a hard gate would redline every CI on day one.
A repo can escalate to `blocking` once tuned. Scoped to the relevant frameworks
and given a skip-list (e.g. External-Secrets `ExternalSecret` CRs trip Checkov's
plaintext-secret checks) so it does not overlap gitleaks (secrets) or hadolint
(Dockerfiles). Findings come from Checkov's SARIF output.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hyperi_ci.common import info, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.tools import missing_tool_notice

_DEFAULT_FRAMEWORKS = ["kubernetes", "helm", "kustomize", "terraform"]
# Never scan the worktree duplicate trees or scratch (regex, matched by Checkov
# --skip-path against the path).
_DEFAULT_SKIP_PATHS = [r".*/\.worktrees/.*", r".*/\.tmp/.*"]


def _resolve_mode(config: CIConfig) -> str:
    """Resolve Checkov's mode: ``warn`` (default) / ``blocking`` / ``disabled``."""
    return resolve_cross_tool_mode(config, "checkov", "warn")


def _base_cmd() -> list[str] | None:
    """Return the checkov invocation (direct or via uvx), or None if absent."""
    if shutil.which("checkov"):
        return ["checkov"]
    if shutil.which("uvx"):
        return ["uvx", "checkov"]
    return None


def run(root: Path, config: CIConfig, *, sarif_path: str | Path | None = None) -> int:
    """Scan ``root`` for IaC misconfigurations. Returns exit code.

    0 = clean / advisory / skipped / missing-tool; 1 = a ``blocking`` gate hit a
    finding. Default mode is ``warn`` (advisory), so day-one it never fails.
    """
    mode = _resolve_mode(config)
    if mode == "disabled":
        info("  checkov: disabled")
        return 0

    base = _base_cmd()
    if base is None:
        # Advisory install path (uvx) - a missing tool warn-skips, never fatal.
        warn(missing_tool_notice("checkov"))
        return 0

    frameworks = config.get("quality.checkov.frameworks", _DEFAULT_FRAMEWORKS)
    if not isinstance(frameworks, list):
        frameworks = _DEFAULT_FRAMEWORKS
    skip_checks = config.get("quality.checkov.skip", [])
    skip_paths = list(_DEFAULT_SKIP_PATHS)
    extra_paths = config.get("quality.checkov.skip_paths", [])
    if isinstance(extra_paths, list):
        skip_paths.extend(str(x) for x in extra_paths)

    with tempfile.TemporaryDirectory(prefix="hyperi-checkov-") as out_dir:
        cmd = [
            *base,
            "-d",
            str(root),
            "--framework",
            *[str(f) for f in frameworks],
            "--output",
            "sarif",
            "--output-file-path",
            out_dir,
            "--soft-fail",  # exit 0 regardless; WE decide the gate from findings
            "--compact",
            "--quiet",
        ]
        for sp in skip_paths:
            cmd += ["--skip-path", sp]
        if isinstance(skip_checks, list):
            for chk in skip_checks:
                cmd += ["--skip-check", str(chk)]

        info(f"  checkov: scanning {root} ({', '.join(str(f) for f in frameworks)})...")
        try:
            run_cmd(cmd, check=False, capture=True)
        except OSError as exc:
            warn(f"  checkov could not be run ({exc}) - advisory only, not failing.")
            return 0

        sarif_file = Path(out_dir) / "results_sarif.sarif"
        text = sarif_file.read_text(encoding="utf-8") if sarif_file.exists() else ""

    found = fdg.parse_sarif(text, "checkov")
    dropped = fdg.surface("checkov", found, sarif_path=sarif_path)
    if dropped:
        info(f"  checkov: +{dropped} more finding(s) in the job summary")

    if not found:
        info("  checkov: no findings")
        return 0
    if mode == "blocking":
        warn(f"  checkov: {len(found)} finding(s) must be fixed")
        return 1
    warn(f"  checkov: {len(found)} finding(s) (non-blocking)")
    return 0
