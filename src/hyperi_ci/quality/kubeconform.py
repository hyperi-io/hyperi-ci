# Project:   HyperI CI
# File:      src/hyperi_ci/quality/kubeconform.py
# Purpose:   kubeconform k8s manifest schema validation (GATE, Path B)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""kubeconform Kubernetes manifest schema validation - the k8s GATE.

A manifest that does not validate against the Kubernetes OpenAPI schema is a
hard error, so kubeconform gates (blocking). It validates RENDERED manifests -
Helm charts must be ``helm template``-d first (see
:mod:`hyperi_ci.quality.render`); this module takes the resulting manifest
files plus any already-plain manifests (Argo CRs, loose YAML).

**CRD surface.** Real clusters carry many CRDs (Gateway API, Strimzi, Redpanda,
CNPG, External Secrets, cert-manager, KEDA, ArgoCD, ...) that are not in the
core schemas. Two mechanisms keep that from turning the gate into all-noise:

* ``-schema-location`` points at the community CRD catalogue (datreeio), so a
  large fraction of CRDs resolve to a real schema and ARE validated. ``default``
  is kept first so the built-in k8s schemas still apply.
* ``-ignore-missing-schemas`` skips (does not fail) a kind with no schema
  anywhere - an unknown CRD is reported ``skipped``, not ``invalid``.

Coverage caveat (surfaced, not hidden, in the spirit of the gitleaks #67 note):
a ``skipped`` resource was NOT schema-checked, and for multi-source ArgoCD apps
the rendered manifest here uses in-repo default values only. A green
kubeconform gate is "no schema violations we could check", not "fully valid".
"""

from __future__ import annotations

import json
import platform
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.install import install_ci_binary
from hyperi_ci.tools import missing_tool_notice

# hyperi-ci:pin tools.kubeconform
_KUBECONFORM_VERSION = "v0.8.0"

# Community CRD schema catalogue. kubeconform expands the templated path per
# resource; a CRD present in the catalogue validates for real, the rest are
# skipped (with -ignore-missing-schemas) rather than failing the gate.
_CRD_CATALOG = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)


def _install_kubeconform() -> str | None:
    """Install the pinned kubeconform release on Linux CI (else None)."""
    arch = "amd64" if platform.machine() in ("x86_64", "AMD64") else "arm64"
    url = (
        f"https://github.com/yannh/kubeconform/releases/download/"
        f"{_KUBECONFORM_VERSION}/kubeconform-linux-{arch}.tar.gz"
    )
    return install_ci_binary("kubeconform", url, tar_member="kubeconform")


def _schema_locations(config: CIConfig) -> list[str]:
    """Schema search path: built-in defaults, the CRD catalogue, then extras.

    A repo adds cluster-specific CRD schema locations via
    ``quality.kubeconform.schema_locations`` (a list) in .hyperi-ci.yaml.
    """
    locs = ["default", _CRD_CATALOG]
    extra = config.get("quality.kubeconform.schema_locations", [])
    if isinstance(extra, list):
        locs.extend(str(x) for x in extra)
    return locs


def _parse(stdout: str) -> list[fdg.Finding]:
    """Parse kubeconform ``-output json`` into findings (invalid/error only).

    kubeconform emits ``{"resources": [{filename, kind, name, version, status,
    msg}], "summary": {...}}``. Only ``invalid`` / ``error`` statuses are
    findings; ``valid`` / ``skipped`` / ``empty`` are not. Status casing varies
    across versions ("INVALID" vs "statusInvalid"), so match case-insensitively
    on the substring.
    """
    try:
        doc = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict):
        return []
    out: list[fdg.Finding] = []
    for r in doc.get("resources", []) or []:
        if not isinstance(r, dict):
            continue
        status = str(r.get("status", "")).lower()
        if "invalid" not in status and "error" not in status:
            continue
        kind = r.get("kind") or "resource"
        name = r.get("name") or ""
        out.append(
            fdg.Finding(
                tool="kubeconform",
                path=str(r.get("filename", "")),
                line=None,
                level="error",
                rule=f"schema/{kind}",
                message=f"{kind} {name}: {r.get('msg', 'schema validation failed')}".strip(),
            )
        )
    return out


def _resolve_mode(config: CIConfig) -> str:
    """Resolve kubeconform's mode: ``blocking`` (default) / ``warn`` / ``disabled``."""
    return resolve_cross_tool_mode(config, "kubeconform", "blocking")


def run(
    manifests: list[Path],
    config: CIConfig,
    *,
    sarif_path: str | Path | None = None,
) -> int:
    """Schema-validate ``manifests`` (rendered + plain). Returns exit code.

    0 = valid / skipped-only / disabled / no manifests; 1 = a blocking gate hit
    an invalid manifest, or the tool is required-but-missing in CI.
    """
    mode = _resolve_mode(config)
    if mode == "disabled":
        info("  kubeconform: disabled")
        return 0
    if not manifests:
        info("  kubeconform: no manifests to validate - skipping")
        return 0

    exe = _install_kubeconform()
    if not exe:
        if mode == "blocking" and is_ci():
            error(missing_tool_notice("kubeconform"))
            return 1
        warn(missing_tool_notice("kubeconform"))
        return 0

    cmd = [exe, "-output", "json", "-summary", "-ignore-missing-schemas"]
    for loc in _schema_locations(config):
        cmd += ["-schema-location", loc]
    cmd += [str(p) for p in manifests]

    info(f"  kubeconform: validating {len(manifests)} manifest file(s)...")
    try:
        result = run_cmd(cmd, check=False, capture=True)
    except OSError as exc:
        warn(f"  kubeconform could not be run ({exc})")
        if mode == "blocking" and is_ci():
            error("  kubeconform could not complete - failing the gate")
            return 1
        return 0
    found = _parse(result.stdout)

    # kubeconform exits 0 valid / 1 invalid (parsed into findings) / other on
    # error. Non-zero with nothing parsed = a tool error (unreadable input, bad
    # schema location), NOT "all valid" - never score a broken gate green.
    if result.returncode != 0 and not found:
        warn(
            f"  kubeconform exited {result.returncode} with no parseable output - tool error, not a clean pass"
        )
        if mode == "blocking" and is_ci():
            error("  kubeconform could not complete - failing the gate")
            return 1
        return 0

    dropped = fdg.surface("kubeconform", found, sarif_path=sarif_path)
    if dropped:
        info(f"  kubeconform: +{dropped} more finding(s) in the job summary")

    if not found:
        success("  kubeconform: all manifests valid (unknown CRDs skipped)")
        return 0
    if mode == "blocking":
        error(f"  kubeconform: {len(found)} invalid manifest(s) must be fixed")
        return 1
    warn(f"  kubeconform: {len(found)} invalid manifest(s) (non-blocking)")
    return 0
