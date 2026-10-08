# Project:   HyperI CI
# File:      src/hyperi_ci/quality/kube_linter.py
# Purpose:   kube-linter k8s best-practice linting (ADVISORY)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""kube-linter Kubernetes best-practice linting - the k8s ADVISORY.

Where kubeconform asks "is this a valid manifest?", kube-linter asks "is this a
GOOD one?" - production-readiness and security best practices (no run-as-root,
resource limits set, liveness/readiness probes, ...). It is advisory: it
surfaces recommendations and NEVER fails the build.

Unlike kubeconform, kube-linter templates Helm charts itself, so it takes the
chart directories and plain manifests directly - no pre-render needed.

hyperi-ci merges one check into the repo's own config,
``liveness-without-startup-probe``: without a startupProbe, a slow start is
restarted by the liveness probe. A repo drops it through ``checks.exclude``.

Findings come from ``--format sarif`` and surface through the shared layer.
"""

import platform
import subprocess
from pathlib import Path

import yaml

from hyperi_ci.common import info, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.install import install_ci_binary
from hyperi_ci.quality.targets import first_file
from hyperi_ci.tools import find_tool
from hyperi_ci.versions import tool_sha256, tool_version

STARTUP_PROBE_CHECK = "liveness-without-startup-probe"

# kube-linter's CEL template reports the string the expression returns, and
# treats an empty string as a pass.
_STARTUP_PROBE_CEL = """\
[(has(object.spec.template) ? object.spec.template.spec :
  has(object.spec.jobTemplate) ? object.spec.jobTemplate.spec.template.spec :
  object.spec).containers.filter(c, has(c.livenessProbe) && !has(c.startupProbe))
].map(bad, bad.size() == 0 ? "" :
  "container \\"" + bad[0].name + "\\" has a livenessProbe and no startupProbe")[0]
"""


def _install_kube_linter() -> str | None:
    """Install the pinned kube-linter release on Linux CI (else None)."""
    # Raw single binary (no tarball) - amd64 is `kube-linter-linux`, arm64 adds
    # the `_arm64` suffix.
    is_amd64 = platform.machine() in ("x86_64", "AMD64")
    suffix = "" if is_amd64 else "_arm64"
    arch = "amd64" if is_amd64 else "arm64"
    url = (
        f"https://github.com/stackrox/kube-linter/releases/download/"
        f"{tool_version('kube-linter')}/kube-linter-linux{suffix}"
    )
    return install_ci_binary(
        "kube-linter", url, expected_sha256=tool_sha256("kube-linter", arch)
    )


def startup_probe_check() -> dict:
    """Return the ``customChecks`` entry for a liveness probe with no startup probe."""
    return {
        "name": STARTUP_PROBE_CHECK,
        "description": (
            "A container with a livenessProbe and no startupProbe is restarted "
            "whenever it starts slower than the liveness budget."
        ),
        "remediation": (
            "Add a startupProbe sized to the container's worst-case start time."
        ),
        "scope": {"objectKinds": ["DeploymentLike"]},
        "template": "cel-expression",
        "params": {"check": _STARTUP_PROBE_CEL},
    }


def merged_config(root: Path, out: Path) -> Path:
    """Write the repo's kube-linter config plus the startup-probe check to ``out``.

    A repo config that cannot be read as a mapping is replaced rather than
    merged, with a warning, so the advisory still runs.
    """
    doc: dict = {}
    path = first_file(root, (".kube-linter.yaml", ".kube-linter.yml"))
    if path is not None:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            warn(f"  kube-linter: {path} could not be read ({exc}) - using defaults")
            loaded = None
        if isinstance(loaded, dict):
            doc = loaded
    custom = doc.get("customChecks")
    if not isinstance(custom, list):
        custom = []
    if not any(
        isinstance(c, dict) and c.get("name") == STARTUP_PROBE_CHECK for c in custom
    ):
        custom.append(startup_probe_check())
    doc["customChecks"] = custom
    out.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    return out


def run(
    targets: list[Path],
    config: CIConfig,
    *,
    root: Path | None = None,
    scratch: Path | None = None,
    sarif_path: str | Path | None = None,
    timeout: float | None = None,
) -> int:
    """Lint ``targets`` (chart dirs + plain manifests). ALWAYS returns 0.

    ``quality.kube_linter: disabled`` turns it off. Otherwise best-practice
    findings surface through the shared layer and the build carries on. With
    ``scratch`` set, the merged config carrying the startup-probe check is
    written there and passed with ``--config``; without it kube-linter reads
    the repo's own config from the working directory.
    """
    if resolve_tool_mode("kube_linter", config, default="warn") == "disabled":
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

    cmd = [exe, "lint", "--format", "sarif"]
    if scratch is not None:
        config_path = merged_config(root or Path.cwd(), scratch / "kube-linter.yaml")
        cmd += ["--config", str(config_path)]
    cmd += [str(p) for p in targets]
    info(f"  kube-linter: advising on {len(targets)} target(s)...")
    try:
        result = run_cmd(
            cmd, check=False, capture=True, timeout=timeout, own_group=True
        )
    except subprocess.TimeoutExpired:
        warn(f"  kube-linter: no result within {timeout}s - advisory only, not failing")
        return 0
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
