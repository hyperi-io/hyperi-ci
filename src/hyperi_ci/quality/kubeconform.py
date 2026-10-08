# Project:   HyperI CI
# File:      src/hyperi_ci/quality/kubeconform.py
# Purpose:   kubeconform k8s manifest schema validation (GATE, Path B)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""kubeconform Kubernetes manifest schema validation, blocking by default.

It takes manifests already rendered by :mod:`hyperi_ci.quality.render` plus
plain ones (Argo CRs, loose YAML). After the built-in schemas it searches the
datreeio CRD catalogue, and ``-ignore-missing-schemas`` reports a CRD with no
schema as ``skipped``, not ``invalid``. ``-strict`` is on unless
``quality.kubeconform.strict`` reads as off. Schemas are cached per
kubeconform pin for at most 7 days.

A green run means no violation it could check: skipped kinds are unchecked,
and multi-source ArgoCD apps render with in-repo defaults only.
"""

import contextlib
import json
import subprocess
import time
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, truthy, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.native_tools import ci_binary
from hyperi_ci.quality import findings as fdg
from hyperi_ci.tools import missing_tool_notice
from hyperi_ci.upgrade import CACHE_DIR
from hyperi_ci.versions import tool_version

# kubeconform expands the template per resource.
_CRD_CATALOG = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

_CACHE_DAYS = 7


def _schema_locations(config: CIConfig) -> list[str]:
    """Return the schema search path: built-in, CRD catalogue, then extras.

    Extras come from the ``quality.kubeconform.schema_locations`` list.
    """
    locs = ["default", _CRD_CATALOG]
    extra = config.get("quality.kubeconform.schema_locations", [])
    if isinstance(extra, list):
        locs.extend(str(x) for x in extra)
    return locs


def _parse(stdout: str) -> list[fdg.Finding]:
    """Parse kubeconform ``-output json`` into findings, invalid and error only.

    Status spelling varies by version ("INVALID", "statusInvalid"), so it is
    matched case-insensitively by substring.
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


def strict(config: CIConfig) -> bool:
    """Return whether ``-strict`` is on: ``quality.kubeconform.strict``, default on.

    ``-strict`` rejects a field the schema does not define and a key set twice,
    the two mistakes the API server silently drops on apply.
    """
    raw = config.get("quality.kubeconform.strict")
    return raw is None or truthy(raw)


def _schema_cache() -> Path:
    """Return the schema cache for this kubeconform pin, with stale entries dropped.

    The CRD catalogue tracks ``main``, so entries older than :data:`_CACHE_DAYS`
    are deleted.
    """
    cache = CACHE_DIR / "kubeconform-schemas" / tool_version("kubeconform")
    cache.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - _CACHE_DAYS * 86400
    for entry in cache.iterdir():
        with contextlib.suppress(OSError):
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
    return cache


def run(
    manifests: list[Path],
    config: CIConfig,
    *,
    sarif_path: str | Path | None = None,
    timeout: float | None = None,
) -> int:
    """Schema-validate ``manifests``, rendered and plain; return the exit code.

    Returns 1 when a blocking gate hits an invalid manifest or a timeout, or in
    CI cannot run or complete; else 0.
    """
    mode = resolve_tool_mode("kubeconform", config, default="blocking")
    if mode == "disabled":
        info("  kubeconform: disabled")
        return 0
    if not manifests:
        info("  kubeconform: no manifests to validate - skipping")
        return 0

    exe = ci_binary("kubeconform")
    if not exe:
        if mode == "blocking" and is_ci():
            error(missing_tool_notice("kubeconform"))
            return 1
        warn(missing_tool_notice("kubeconform"))
        return 0

    cmd = [exe, "-output", "json", "-summary", "-ignore-missing-schemas"]
    cmd += ["-cache", str(_schema_cache())]
    if strict(config):
        cmd.append("-strict")
    for loc in _schema_locations(config):
        cmd += ["-schema-location", loc]
    cmd += [str(p) for p in manifests]

    info(f"  kubeconform: validating {len(manifests)} manifest file(s)...")
    try:
        result = run_cmd(
            cmd, check=False, capture=True, timeout=timeout, own_group=True
        )
    except subprocess.TimeoutExpired:
        message = f"  kubeconform: no result within {timeout}s"
        if mode == "blocking":
            error(f"{message} - failing the gate")
            return 1
        warn(message)
        return 0
    except OSError as exc:
        warn(f"  kubeconform could not be run ({exc})")
        if mode == "blocking" and is_ci():
            error("  kubeconform could not complete - failing the gate")
            return 1
        return 0
    found = _parse(result.stdout)

    # A failing exit with nothing parsed is a tool error, not a valid tree.
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
