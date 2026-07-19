# Project:   HyperI CI
# File:      src/hyperi_ci/quality/lint_manifests.py
# Purpose:   Orchestrate the k8s + IaC linting dimension (Path B)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Orchestrate k8s + IaC linting for a gitops / infra repo (Path B).

This is what the ``hyperi-ci lint-manifests <dir>`` verb runs. It is the
entry point for GitHub-Actions-native gitops repos (dfe-infra has no
``.hyperi-ci.yaml`` and no language pipeline), so their existing workflows can
call one command instead of adopting the whole hyperi-ci pipeline.

Pipeline:

1. discover Helm charts + plain (already-rendered) manifests, pruning
   ``.worktrees`` duplicate trees and chart internals;
2. ``helm template`` the charts (kubeconform needs rendered manifests);
3. **kubeconform** - schema-validation GATE over rendered charts + plain
   manifests (its exit code is the verb's exit code);
4. **kube-linter** - best-practice ADVISORY over charts + manifests (kube-linter
   templates Helm itself);
5. **Checkov** - IaC security ADVISORY over the whole tree (k8s/helm/kustomize/
   terraform).

Only kubeconform gates; the advisories always run (even when the gate fails,
so their findings still surface) and never change the exit code.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hyperi_ci.common import error, get_exclude_dirs, group, info, is_ci, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import checkov, kube_linter, kubeconform, render
from hyperi_ci.quality.targets import discover_helm_charts, discover_manifests


def run(
    root: Path | str, config: CIConfig, *, sarif_path: str | Path | None = None
) -> int:
    """Lint every chart / manifest / IaC file under ``root``.

    Returns non-zero when a GATE fails: kubeconform (a schema-invalid manifest)
    OR a Checkov set to ``blocking``. kube-linter is always advisory and never
    affects the exit code; Checkov is advisory by DEFAULT (``warn``) and only
    gates when a repo has escalated it. All tools always RUN (even after a gate
    fails) so their findings still surface.
    """
    root = Path(root)
    exclude = get_exclude_dirs(config._raw)
    charts = discover_helm_charts(root, exclude_dirs=exclude)
    manifests = discover_manifests(root, exclude_dirs=exclude)

    if not charts and not manifests:
        info(f"lint-manifests: no Helm charts or k8s manifests under {root} - skipping")
        # Still run Checkov: a repo may be pure IaC (.tf) with no k8s at all.
        with group("checkov IaC security advisory"):
            return _run_checkov(root, config, sarif_path)

    info(
        f"lint-manifests: {len(charts)} chart(s), {len(manifests)} plain manifest(s) under {root}"
    )

    gate_rc = 0
    with tempfile.TemporaryDirectory(prefix="hyperi-lint-manifests-") as tmp:
        rendered: list[Path] = []
        if charts:
            if render.helm_available():
                with group("Render Helm charts"):
                    rendered = render.render_charts(charts, Path(tmp) / "rendered")
            else:
                warn(
                    "  helm not on PATH - charts will NOT be schema-validated "
                    "(kube-linter/checkov still cover them). Install helm for the gate."
                )
        # A chart that did not render - helm absent, OR a `helm template` failure
        # for that chart - was NOT schema-validated. A blocking gate must not
        # pass green having skipped charts (design principle 3, No silent skips).
        unrendered = len(charts) - len(rendered)
        with group("kubeconform schema validation (gate)"):
            gate_rc = kubeconform.run(
                rendered + manifests, config, sarif_path=sarif_path
            )
        if (
            unrendered
            and is_ci()
            and resolve_cross_tool_mode(config, "kubeconform", "blocking") == "blocking"
        ):
            error(
                f"  kubeconform: {unrendered} of {len(charts)} chart(s) could not be "
                "rendered/validated and the gate is blocking - failing rather than "
                "pass unchecked charts"
            )
            gate_rc = gate_rc or 1

    with group("kube-linter best-practice advisory"):
        kube_linter.run([*charts, *manifests], config, sarif_path=sarif_path)

    with group("checkov IaC security advisory"):
        checkov_rc = _run_checkov(root, config, sarif_path)

    # A failing gate wins: kubeconform OR a blocking Checkov fails the verb.
    return gate_rc or checkov_rc


def _run_checkov(root: Path, config: CIConfig, sarif_path: str | Path | None) -> int:
    """Run the Checkov advisory (kept separate so the no-manifests path reuses it)."""
    return checkov.run(root, config, sarif_path=sarif_path)
