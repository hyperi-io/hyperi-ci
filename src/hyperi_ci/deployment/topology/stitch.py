# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/topology/stitch.py
# Purpose:   Stitch a DeploymentTopology into an umbrella Helm chart
#
# License:   Proprietary - HYPERI PTY LIMITED
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

    Implementation lands in Commit B. This signature is stable.
    """
    raise NotImplementedError("stitch_topology lands in Commit B")
