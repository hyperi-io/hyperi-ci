# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/topology/stitch.py
# Purpose:   Stitch a DeploymentTopology into an umbrella Helm chart
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Topology stitcher - composes per-app charts into an umbrella chart.

Reads a DeploymentTopology + resolved chart versions, generates the
umbrella's Chart.yaml + values.yaml, copies glue templates into
templates/, runs ``helm dep update`` + ``helm lint``.

The output directory is a complete Helm chart ready for ``helm package``.

Data types come from pylib (``hyperi_pylib.deployment.topology``); this
module holds only the operational stitching logic.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from hyperi_pylib.deployment.topology import (
    AppEntry,
    DeploymentTopology,
    ThirdPartyEntry,
)
from hyperi_pylib.deployment.topology.errors import TopologyError


@dataclass
class StitchResult:
    """Outcome of a stitch operation."""

    chart_dir: Path
    chart_yaml: Path
    resolved_versions: dict[str, str] = field(default_factory=dict)
    glue_copied: list[Path] = field(default_factory=list)


def generate_chart_yaml(
    topology: DeploymentTopology,
    *,
    resolved: dict[str, str],
    oci_base: str,
) -> str:
    """Generate the umbrella ``Chart.yaml`` content as a YAML string.

    Args:
        topology: The DeploymentTopology declaration.
        resolved: ``{chart-name: concrete-version}`` map from the resolver.
        oci_base: OCI registry URL for per-app charts (third-party charts
            override via their own ``repository`` field).

    Returns:
        ``Chart.yaml`` content as a UTF-8 string with LF line endings.

    """
    deps: list[dict[str, object]] = []
    for app in topology.spec.apps:
        deps.append(_app_dep(app, resolved=resolved, oci_base=oci_base))
    for tp in topology.spec.thirdParty:
        deps.append(_third_party_dep(tp, resolved=resolved))

    chart = {
        "apiVersion": "v2",
        "name": topology.spec.umbrella.name,
        "description": topology.spec.umbrella.description,
        "type": "application",
        "version": _chart_version(topology),
        "appVersion": topology.spec.umbrella.appVersion,
        "dependencies": deps,
    }
    return yaml.safe_dump(chart, sort_keys=False, default_flow_style=False)


def _app_dep(
    app: AppEntry,
    *,
    resolved: dict[str, str],
    oci_base: str,
) -> dict[str, object]:
    if app.name not in resolved:
        raise TopologyError(f"resolver did not return a version for {app.name}")
    dep: dict[str, object] = {
        "name": app.name,
        "version": resolved[app.name],
        "repository": oci_base,
        "condition": app.condition or f"{app.alias or app.name}.enabled",
    }
    if app.alias:
        dep["alias"] = app.alias
    return dep


def _third_party_dep(
    tp: ThirdPartyEntry,
    *,
    resolved: dict[str, str],
) -> dict[str, object]:
    if tp.name not in resolved:
        raise TopologyError(f"resolver did not return a version for {tp.name}")
    dep: dict[str, object] = {
        "name": tp.name,
        "version": resolved[tp.name],
        "repository": tp.repository,
        "condition": tp.condition or f"{tp.alias or tp.name}.enabled",
    }
    if tp.alias:
        dep["alias"] = tp.alias
    return dep


def _chart_version(topology: DeploymentTopology) -> str:
    """Umbrella chart version.

    Defaults to ``umbrella.appVersion`` if not otherwise specified.
    Future: derive from semantic-release on the topology directory.
    """
    return topology.spec.umbrella.appVersion


def stitch_topology(
    topology: DeploymentTopology,
    *,
    topology_dir: Path,
    output_dir: Path,
    resolved: dict[str, str],
    oci_base: str,
    run_helm_dep_update: bool = True,
    run_helm_lint: bool = True,
) -> StitchResult:
    """Stitch a topology into a complete umbrella chart in ``output_dir``.

    Args:
        topology: Parsed DeploymentTopology.
        topology_dir: Topology source dir (contains topology.yaml, optional
            values.yaml + glue/*).
        output_dir: Destination chart dir (will be created; overwritten if
            it exists).
        resolved: ``{chart-name: concrete-version}`` from the resolver.
        oci_base: OCI URL for per-app charts.
        run_helm_dep_update: Invoke ``helm dep update`` after stitching.
        run_helm_lint: Invoke ``helm lint`` after stitching.

    Returns:
        :class:`StitchResult` with paths + resolved version table.

    Raises:
        TopologyError: glue file missing, helm tooling failure.

    """
    # Fresh output directory
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    (output_dir / "templates").mkdir()

    # Chart.yaml
    chart_yaml = generate_chart_yaml(topology, resolved=resolved, oci_base=oci_base)
    chart_path = output_dir / "Chart.yaml"
    chart_path.write_text(chart_yaml, encoding="utf-8", newline="\n")

    # values.yaml - copy from topology dir if present, else generate minimal
    src_values = topology_dir / "values.yaml"
    if src_values.exists():
        shutil.copy2(src_values, output_dir / "values.yaml")
    else:
        _write_minimal_values(topology, output_dir / "values.yaml")

    # Copy per-env values files alongside (values.dev.yaml etc.)
    for env_values in topology_dir.glob("values.*.yaml"):
        shutil.copy2(env_values, output_dir / env_values.name)

    # Copy glue templates
    glue_copied: list[Path] = []
    for glue in topology.spec.glue:
        src_glue = topology_dir / glue.file
        if not src_glue.exists():
            raise TopologyError(
                f"glue file missing: {src_glue} (referenced by topology entry {glue.name!r})"
            )
        dst_glue = output_dir / "templates" / Path(glue.file).name
        shutil.copy2(src_glue, dst_glue)
        glue_copied.append(dst_glue)

    # .helmignore
    (output_dir / ".helmignore").write_text(
        _DEFAULT_HELMIGNORE, encoding="utf-8", newline="\n"
    )

    # RESOLVED.md - diff visibility
    resolved_md = "# Resolved chart versions\n\n"
    resolved_md += "| Chart | Resolved version |\n|---|---|\n"
    for chart, ver in sorted(resolved.items()):
        resolved_md += f"| {chart} | {ver} |\n"
    (output_dir / "RESOLVED.md").write_text(resolved_md, encoding="utf-8", newline="\n")

    # helm dep update + helm lint
    if run_helm_dep_update:
        _helm_dep_update(output_dir)
    if run_helm_lint:
        _helm_lint(output_dir)

    return StitchResult(
        chart_dir=output_dir,
        chart_yaml=chart_path,
        resolved_versions=dict(resolved),
        glue_copied=glue_copied,
    )


def _write_minimal_values(topology: DeploymentTopology, path: Path) -> None:
    """Write a minimal values.yaml enabling every sub-chart by alias/name."""
    values: dict[str, object] = {}
    for app in topology.spec.apps:
        key = app.alias or app.name
        values[key] = {"enabled": app.enabled}
    for tp in topology.spec.thirdParty:
        key = tp.alias or tp.name
        values[key] = {"enabled": tp.enabled}
    path.write_text(
        yaml.safe_dump(values, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
        newline="\n",
    )


def _helm_dep_update(chart_dir: Path) -> None:
    """Run ``helm dep update`` on the stitched chart."""
    if shutil.which("helm") is None:
        raise TopologyError("`helm` binary not on PATH; cannot run dep update")
    proc = subprocess.run(
        ["helm", "dep", "update", str(chart_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise TopologyError(
            f"helm dep update failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def _helm_lint(chart_dir: Path) -> None:
    """Run ``helm lint`` on the stitched chart."""
    if shutil.which("helm") is None:
        raise TopologyError("`helm` binary not on PATH; cannot run lint")
    proc = subprocess.run(
        ["helm", "lint", str(chart_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise TopologyError(
            f"helm lint failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


_DEFAULT_HELMIGNORE = """\
# Generated by hyperi-ci stitch
.DS_Store
.git/
.gitignore
.bzr/
.hg/
.svn/
*.swp
*.bak
*.tmp
*.orig
*~
.project
.idea/
*.tmproj
.vscode/
"""
