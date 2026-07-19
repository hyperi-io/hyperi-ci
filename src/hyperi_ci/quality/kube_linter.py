# Project:   HyperI CI
# File:      src/hyperi_ci/quality/kube_linter.py
# Purpose:   kube-linter k8s best-practice linting (ADVISORY, Path B)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""kube-linter Kubernetes best-practice linting - the k8s ADVISORY.

Where kubeconform asks "is this a valid manifest?", kube-linter asks "is this a
GOOD one?" - production-readiness and security best practices (no run-as-root,
resource limits set, liveness/readiness probes, ...). It is advisory: it
surfaces recommendations and NEVER fails the build.

Unlike kubeconform, kube-linter templates Helm charts itself, so it takes the
chart directories and plain manifests directly - no pre-render needed. It
positions naturally on the RENDERED/chart side, adding the best-practice checks
that plain ``helm lint`` does not do. A repo's own ``.kube-linter.yaml`` (auto-
discovered) tunes the checks.

Findings come from ``--format sarif`` and surface through the shared layer.
"""

from __future__ import annotations

import platform
from pathlib import Path

from hyperi_ci.common import info, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.install import install_ci_binary
from hyperi_ci.tools import find_tool

# hyperi-ci:pin tools.kube-linter
_KUBE_LINTER_VERSION = "v0.8.3"


def _install_kube_linter() -> str | None:
    """Install the pinned kube-linter release on Linux CI (else None)."""
    # Raw single binary (no tarball) - amd64 is `kube-linter-linux`, arm64 adds
    # the `_arm64` suffix.
    suffix = "" if platform.machine() in ("x86_64", "AMD64") else "_arm64"
    url = (
        f"https://github.com/stackrox/kube-linter/releases/download/"
        f"{_KUBE_LINTER_VERSION}/kube-linter-linux{suffix}"
    )
    return install_ci_binary("kube-linter", url)


def run(
    targets: list[Path],
    config: CIConfig,
    *,
    sarif_path: str | Path | None = None,
) -> int:
    """Lint ``targets`` (chart dirs + plain manifests). ALWAYS returns 0.

    ``quality.kube_linter: disabled`` turns it off. Otherwise best-practice
    findings surface through the shared layer and the build carries on.
    """
    if resolve_cross_tool_mode(config, "kube_linter", "warn") == "disabled":
        info("  kube-linter: disabled")
        return 0
    if not targets:
        info("  kube-linter: no charts or manifests - skipping")
        return 0

    # Auto-install on Linux CI; fall back to an already-present binary. Advisory,
    # so a missing tool info-skips rather than failing.
    exe = _install_kube_linter() or find_tool("kube-linter", recommended=False)
    if not exe:
        return 0

    cmd = [exe, "lint", "--format", "sarif", *[str(p) for p in targets]]
    info(f"  kube-linter: advising on {len(targets)} target(s)...")
    try:
        result = run_cmd(cmd, check=False, capture=True)
    except OSError as exc:
        warn(f"  kube-linter could not be run ({exc}) - advisory only, not failing.")
        return 0

    found = fdg.parse_sarif(result.stdout, "kube-linter")
    dropped = fdg.surface("kube-linter", found, sarif_path=sarif_path)
    if found:
        warn(f"  kube-linter: {len(found)} advisory finding(s)")
        if dropped:
            info(f"  kube-linter: +{dropped} more in the job summary")
    return 0
