# Project:   HyperI CI
# File:      src/hyperi_ci/quality/kube_linter.py
# Purpose:   kube-linter k8s best-practice linting (ADVISORY)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""kube-linter Kubernetes best-practice linting; advisory, never fails a build.

lint-iac hands it the Helm renders kubeconform validates, because kube-linter's
own templating silently skips a chart with no ``values.yaml``. hyperi-ci merges
``liveness-without-startup-probe`` into the repo's config; a repo drops it
through ``checks.exclude``. kube-linter exits 0 or 1 with no report when nothing
loads or the config is rejected, so skipped targets and a missing report are
findings too.
"""

import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import yaml

from hyperi_ci.common import info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.native_tools import ci_binary
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.targets import first_file
from hyperi_ci.tools import find_tool

STARTUP_PROBE_CHECK = "liveness-without-startup-probe"

# What ``--verbose`` writes to stderr for each target it skips.
_LOAD_FAILED = re.compile(
    r"^Warning: failed to load object from (?P<path>.+?): (?P<why>.+)$", re.MULTILINE
)

# kube-linter's CEL template reports the string the expression returns, and
# treats an empty string as a pass.
_STARTUP_PROBE_CEL = """\
[(has(object.spec.template) ? object.spec.template.spec :
  has(object.spec.jobTemplate) ? object.spec.jobTemplate.spec.template.spec :
  object.spec).containers.filter(c, has(c.livenessProbe) && !has(c.startupProbe))
].map(bad, bad.size() == 0 ? "" :
  "container \\"" + bad[0].name + "\\" has a livenessProbe and no startupProbe")[0]
"""


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


def run_problems(stdout: str, stderr: str, returncode: int) -> list[fdg.Finding]:
    """Return a finding per target kube-linter skipped, or one for a missing report.

    Reads a ``--verbose`` run. A run with no SARIF and no skipped target is
    reported once, with kube-linter's last stderr line as the reason.
    """
    problems = [
        fdg.Finding(
            "kube-linter",
            fdg.relpath(Path(m["path"])),
            None,
            "warning",
            "kube-linter/load-failed",
            f"kube-linter skipped this target: {m['why']}",
        )
        for m in _LOAD_FAILED.finditer(stderr)
    ]
    if stdout.strip() or problems:
        return problems
    lines = [line for line in stderr.splitlines() if line.strip()]
    why = lines[-1] if lines else f"exited {returncode}"
    return [
        fdg.Finding(
            "kube-linter",
            "",
            None,
            "warning",
            "kube-linter/no-report",
            f"kube-linter produced no report: {why}",
        )
    ]


def relocate(
    found: list[fdg.Finding], sources: Mapping[Path, Path]
) -> list[fdg.Finding]:
    """Move each finding in a rendered file to its source, dropping the line.

    Render line numbers count the rendered stream, not any template.
    """
    by_render = {out.resolve(): fdg.relpath(src) for out, src in sources.items()}
    moved: list[fdg.Finding] = []
    for f in found:
        source = by_render.get(Path(f.path).resolve()) if f.path else None
        moved.append(f if source is None else replace(f, path=source, line=None))
    return moved


def run(
    targets: list[Path],
    config: CIConfig,
    *,
    root: Path | None = None,
    scratch: Path | None = None,
    sarif_path: str | Path | None = None,
    timeout: float | None = None,
    sources: Mapping[Path, Path] | None = None,
) -> int:
    """Lint ``targets`` (rendered manifests, plain manifests, chart dirs); return 0.

    With ``scratch``, the merged config is written there and passed with
    ``--config``; without it kube-linter reads the repo's own. ``sources`` maps
    a render to the chart or kustomization its findings are reported against.
    """
    if resolve_tool_mode("kube_linter", config, default="warn") == "disabled":
        info("  kube-linter: disabled")
        return 0
    if not targets:
        info("  kube-linter: no charts or manifests - skipping")
        return 0

    exe = ci_binary("kube-linter") or find_tool("kube-linter", recommended=False)
    if not exe:
        return 0

    cmd = [exe, "lint", "--format", "sarif", "--verbose"]
    if scratch is not None:
        config_path = merged_config(root or Path.cwd(), scratch / "kube-linter.yaml")
        cmd += ["--config", str(config_path)]
    cmd += [str(p) for p in targets]
    info(f"  kube-linter: advising on {len(targets)} target(s)...")
    # Run at warn: a timeout or a run that never started only warns.
    found = fdg.run_check(
        "kube-linter",
        cmd,
        "warn",
        lambda result: (
            fdg.parse_sarif(result.stdout, "kube-linter")
            + run_problems(result.stdout, result.stderr, result.returncode)
        ),
        timeout=timeout,
    )
    if isinstance(found, int):
        return 0
    if sources:
        found = relocate(found, sources)
    # Advisory in every mode, so its findings surface as a check at warn would.
    fdg.report("kube-linter", found, "warn", sarif_path=sarif_path)
    if found:
        warn(f"  kube-linter: {len(found)} advisory finding(s)")
    return 0
