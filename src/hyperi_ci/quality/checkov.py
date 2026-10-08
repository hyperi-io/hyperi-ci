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

Installed the same way as semgrep - `uvx --from checkov==<pin>`, with the pin in
versions.yaml, since uv is already a hard dependency of every hyperi-ci project.

**Advisory by default** (`warn`): its 1000+ policies are broad, and retrofitting
them onto an existing estate as a hard gate would redline every CI on day one.
A repo can escalate to `blocking` once tuned. Scoped to the relevant frameworks
and given a skip-list (e.g. External-Secrets `ExternalSecret` CRs trip Checkov's
plaintext-secret checks) so it does not overlap gitleaks (secrets) or hadolint
(Dockerfiles). Findings come from Checkov's SARIF output.
"""

import dataclasses
import shutil
import subprocess
import tempfile
from pathlib import Path

from hyperi_ci.common import info, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_cmd, resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.tools import missing_tool_notice
from hyperi_ci.versions import tool_version

_DEFAULT_FRAMEWORKS = ["kubernetes", "helm", "kustomize", "terraform"]
# Never scan the worktree duplicate trees or scratch (regex, matched by Checkov
# --skip-path against the path).
_DEFAULT_SKIP_PATHS = [r".*/\.worktrees/.*", r".*/\.tmp/.*"]


def _base_cmd() -> list[str] | None:
    """Return the pinned checkov invocation, or None when nothing can run it.

    The pin from versions.yaml runs through uvx wherever uv exists; a PATH copy
    is the fallback without uv.
    """
    cmd = resolve_tool_cmd(
        ["checkov"], use_uvx=True, spec=f"checkov=={tool_version('checkov')}"
    )
    return cmd if shutil.which(cmd[0]) else None


def _in_tree(finding: fdg.Finding, root: Path) -> fdg.Finding:
    """Point a finding at the longest tail of its path that exists under ``root``.

    Checkov names a rendered Helm template through its own temp dir
    (``tmpab12cd34/charts/app/templates/x.yaml``) whenever ``root`` and that
    temp dir share an ancestor below ``/``, as a scratch copy under TMPDIR does.
    """
    parts = Path(finding.path).parts
    for start in range(len(parts)):
        tail = Path(*parts[start:])
        if (root / tail).exists():
            return dataclasses.replace(finding, path=tail.as_posix())
    return finding


def run(
    root: Path,
    config: CIConfig,
    *,
    sarif_path: str | Path | None = None,
    timeout: float | None = None,
    memory_limit_bytes: int | None = None,
) -> int:
    """Scan ``root`` for IaC misconfigurations. Returns exit code.

    0 = clean / advisory / skipped / missing-tool; 1 = a ``blocking`` gate hit a
    finding, or did not finish within ``timeout``. Default mode is ``warn``
    (advisory), so day-one it never fails. ``memory_limit_bytes`` caps
    Checkov's address space.
    """
    mode = resolve_tool_mode("checkov", config, default="warn")
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
        problem = None
        try:
            result = run_cmd(
                cmd,
                check=False,
                capture=True,
                timeout=timeout,
                memory_limit_bytes=memory_limit_bytes,
                own_group=True,
            )
        except subprocess.TimeoutExpired:
            problem = f"  checkov: no result within {timeout}s"
        except OSError as exc:
            warn(f"  checkov could not be run ({exc}) - advisory only, not failing.")
            return 0
        else:
            sarif_file = Path(out_dir) / "results_sarif.sarif"
            text = sarif_file.read_text(encoding="utf-8") if sarif_file.exists() else ""
            # --soft-fail exits 0 on findings: a failing exit with no report is a crash.
            if result.returncode != 0 and not text:
                tail = (result.stderr or result.stdout).strip().splitlines()
                detail = tail[-1] if tail else "no output"
                problem = (
                    f"  checkov exited {result.returncode} with no report ({detail})"
                )

    if problem is not None:
        if mode == "blocking":
            warn(f"{problem} - failing the gate")
            return 1
        warn(f"{problem} - advisory only, not failing")
        return 0

    found = [_in_tree(f, root) for f in fdg.parse_sarif(text, "checkov")]
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
