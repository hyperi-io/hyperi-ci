"""Tests for the topology stitcher."""

from __future__ import annotations

import yaml
from hyperi_pylib.deployment.topology import (
    AppEntry,
    DeploymentTopology,
    ThirdPartyEntry,
    TopologySpec,
    UmbrellaMeta,
)

from hyperi_ci.deployment.topology.stitch import generate_chart_yaml


def _make_topology() -> DeploymentTopology:
    return DeploymentTopology(
        metadata={"name": "default"},
        spec=TopologySpec(
            umbrella=UmbrellaMeta(
                name="hyperi-deployment-default",
                description="HyperI default rollout",
                appVersion="1.0",
            ),
            apps=[
                AppEntry(name="dfe-loader", version="^1.18"),
                AppEntry(name="dfe-receiver", version="^1.15"),
            ],
            thirdParty=[
                ThirdPartyEntry(
                    name="strimzi-kafka-operator",
                    repository="oci://quay.io/strimzi-helm",
                    version="0.45.0",
                    alias="strimzi",
                ),
            ],
        ),
    )


def test_generate_chart_yaml_top_level_fields():
    topo = _make_topology()
    resolved = {
        "dfe-loader": "1.18.3",
        "dfe-receiver": "1.15.2",
        "strimzi-kafka-operator": "0.45.0",
    }
    out = generate_chart_yaml(
        topo,
        resolved=resolved,
        oci_base="oci://ghcr.io/hyperi-io/helm-charts",
    )
    parsed = yaml.safe_load(out)
    assert parsed["apiVersion"] == "v2"
    assert parsed["name"] == "hyperi-deployment-default"
    assert parsed["description"] == "HyperI default rollout"
    assert parsed["appVersion"] == "1.0"
    assert parsed["type"] == "application"


def test_generate_chart_yaml_dependencies():
    topo = _make_topology()
    resolved = {
        "dfe-loader": "1.18.3",
        "dfe-receiver": "1.15.2",
        "strimzi-kafka-operator": "0.45.0",
    }
    out = generate_chart_yaml(
        topo,
        resolved=resolved,
        oci_base="oci://ghcr.io/hyperi-io/helm-charts",
    )
    parsed = yaml.safe_load(out)
    deps = parsed["dependencies"]
    assert len(deps) == 3

    loader_dep = next(d for d in deps if d["name"] == "dfe-loader")
    assert loader_dep["version"] == "1.18.3"
    assert loader_dep["repository"] == "oci://ghcr.io/hyperi-io/helm-charts"
    assert loader_dep["condition"] == "dfe-loader.enabled"

    strimzi_dep = next(d for d in deps if d["name"] == "strimzi-kafka-operator")
    assert strimzi_dep["version"] == "0.45.0"
    assert strimzi_dep["repository"] == "oci://quay.io/strimzi-helm"
    assert strimzi_dep["alias"] == "strimzi"
    assert strimzi_dep["condition"] == "strimzi.enabled"


def test_generate_chart_yaml_pins_resolved_versions():
    topo = _make_topology()
    resolved = {
        "dfe-loader": "1.18.3",
        "dfe-receiver": "1.15.2",
        "strimzi-kafka-operator": "0.45.0",
    }
    out = generate_chart_yaml(
        topo,
        resolved=resolved,
        oci_base="oci://ghcr.io/hyperi-io/helm-charts",
    )
    assert "^1.18" not in out
    assert "^1.15" not in out
    assert "1.18.3" in out
    assert "1.15.2" in out
