# Project:   HyperI CI
# File:      src/hyperi_ci/quality/render.py
# Purpose:   Render Helm charts to plain manifests for schema validation
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Render Helm charts to plain manifests.

kubeconform validates RENDERED manifests, not Go templates, so a chart must be
``helm template``-d first. kube-linter templates Helm itself, so this is only
for the kubeconform gate. dfe-infra's charts vendor their ``dfe-common``
dependency as a committed ``.tgz`` under ``charts/`` with a ``Chart.lock``, so
``helm dependency build`` resolves offline from what is already in the repo.

Best-effort by design: a chart that fails to render (missing values, a bad
dependency) is warned about and skipped rather than aborting the whole lint -
the caller decides whether an unrenderable chart is fatal.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from hyperi_ci.common import info, run_cmd, warn


def _release_name(chart: Path) -> str:
    """A valid Helm release name derived from the chart dir.

    Helm release names must be lowercase RFC1123-ish (``[a-z0-9-]``, start
    alnum, <=53 chars). The chart DIRECTORY basename is not constrained that way
    - a ``Chart/`` (capital) or ``my_chart/`` (underscore) dir makes
    ``helm template`` fail with "invalid release name", which would silently
    skip the chart and let the schema gate pass green having validated nothing
    (a "No silent skips" violation). The release name is irrelevant to schema
    validation, so sanitise the basename to something always-valid.
    """
    name = re.sub(r"[^a-z0-9-]", "-", chart.name.lower()).strip("-")
    return (name or "chart")[:53]


def helm_available() -> bool:
    """True when the ``helm`` binary is on PATH."""
    return shutil.which("helm") is not None


def render_charts(charts: list[Path], out_dir: Path) -> list[Path]:
    """Render each chart to ``out_dir/<chart>.rendered.yaml``; return the paths.

    Runs ``helm dependency build`` (best-effort - many charts have no deps) then
    ``helm template``. A chart that will not render is warned and skipped.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []
    for chart in charts:
        # dependency build is best-effort: charts with no dependencies exit
        # non-zero on some helm versions, which must not abort the render.
        run_cmd(["helm", "dependency", "build", str(chart)], check=False, capture=True)
        result = run_cmd(
            ["helm", "template", _release_name(chart), str(chart)],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            warn(f"  render: helm template failed for {chart.name} - skipping")
            continue
        dest = out_dir / f"{chart.name}.rendered.yaml"
        dest.write_text(result.stdout, encoding="utf-8", newline="\n")
        rendered.append(dest)
    info(f"  render: {len(rendered)}/{len(charts)} chart(s) templated")
    return rendered
