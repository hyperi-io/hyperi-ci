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

# sha256 pinned from kube-linter v0.8.3 linux release, verified before exec.
# Raw binary, hashed as-is; keyed by arch (amd64 = kube-linter-linux, arm64 =
# kube-linter-linux_arm64). Cross-checked against the release sigstore bundles.
_KUBE_LINTER_SHA256 = {
    "amd64": "618d299a3e2839c8ca9d86fce0db617be0fba41f0fecbbbfb7fbf1c04299fae1",
    "arm64": "9c39d35252e0dcafb16b26197b9e93ba578e44eb402c3c6660fc94e08f94094f",
}


def _install_kube_linter() -> str | None:
    """Install the pinned kube-linter release on Linux CI (else None)."""
    # Raw single binary (no tarball) - amd64 is `kube-linter-linux`, arm64 adds
    # the `_arm64` suffix.
    is_amd64 = platform.machine() in ("x86_64", "AMD64")
    suffix = "" if is_amd64 else "_arm64"
    arch = "amd64" if is_amd64 else "arm64"
    url = (
        f"https://github.com/stackrox/kube-linter/releases/download/"
        f"{_KUBE_LINTER_VERSION}/kube-linter-linux{suffix}"
    )
    return install_ci_binary(
        "kube-linter", url, expected_sha256=_KUBE_LINTER_SHA256[arch]
    )


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
