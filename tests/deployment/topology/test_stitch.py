"""Tests for the topology stitcher."""

from __future__ import annotations

import pytest
import yaml
from scalo.deployment.topology import (
    AppEntry,
    DeploymentTopology,
    GlueEntry,
    ThirdPartyEntry,
    TopologySpec,
    UmbrellaMeta,
)
from scalo.deployment.topology.errors import TopologyError

from hyperi_ci.deployment.topology.stitch import (
    generate_chart_yaml,
    stitch_topology,
)


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


def test_stitch_topology_writes_complete_chart(tmp_path):
    """End-to-end stitch produces Chart.yaml + values.yaml + templates/."""
    topo = DeploymentTopology(
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
            glue=[
                GlueEntry(name="kafka-cluster", file="glue/kafka-cluster.yaml"),
            ],
        ),
    )
    topo_dir = tmp_path / "topology"
    topo_dir.mkdir()
    (topo_dir / "values.yaml").write_text(
        "dfe-loader:\n  enabled: true\n  replicaCount: 2\n",
        encoding="utf-8",
    )
    (topo_dir / "glue").mkdir()
    (topo_dir / "glue" / "kafka-cluster.yaml").write_text(
        "apiVersion: kafka.strimzi.io/v1beta2\nkind: Kafka\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "stitched"
    result = stitch_topology(
        topo,
        topology_dir=topo_dir,
        output_dir=out_dir,
        resolved={
            "dfe-loader": "1.18.3",
            "dfe-receiver": "1.15.2",
            "strimzi-kafka-operator": "0.45.0",
        },
        oci_base="oci://ghcr.io/hyperi-io/helm-charts",
        run_helm_dep_update=False,
        run_helm_lint=False,
    )

    assert (out_dir / "Chart.yaml").exists()
    assert (out_dir / "values.yaml").exists()
    assert (out_dir / "templates" / "kafka-cluster.yaml").exists()
    assert (out_dir / ".helmignore").exists()
    assert (out_dir / "RESOLVED.md").exists()
    assert result.chart_dir == out_dir
    assert result.resolved_versions["dfe-loader"] == "1.18.3"
    assert len(result.glue_copied) == 1


def test_stitch_topology_with_no_values_file_creates_minimal(tmp_path):
    """If topology dir has no values.yaml, stitcher writes a minimal one."""
    topo = _make_topology()
    topo_dir = tmp_path / "topology"
    topo_dir.mkdir()
    out_dir = tmp_path / "stitched"

    stitch_topology(
        topo,
        topology_dir=topo_dir,
        output_dir=out_dir,
        resolved={
            "dfe-loader": "1.18.3",
            "dfe-receiver": "1.15.2",
            "strimzi-kafka-operator": "0.45.0",
        },
        oci_base="oci://ghcr.io/hyperi-io/helm-charts",
        run_helm_dep_update=False,
        run_helm_lint=False,
    )
    values = yaml.safe_load((out_dir / "values.yaml").read_text())
    assert values["dfe-loader"]["enabled"] is True
    assert values["dfe-receiver"]["enabled"] is True
    assert values["strimzi"]["enabled"] is True


def test_stitch_topology_rejects_missing_glue_file(tmp_path):
    topo = DeploymentTopology(
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
            glue=[
                GlueEntry(name="kafka-cluster", file="glue/missing.yaml"),
            ],
        ),
    )
    topo_dir = tmp_path / "topology"
    topo_dir.mkdir()
    out_dir = tmp_path / "stitched"

    with pytest.raises(TopologyError) as ei:
        stitch_topology(
            topo,
            topology_dir=topo_dir,
            output_dir=out_dir,
            resolved={
                "dfe-loader": "1.18.3",
                "dfe-receiver": "1.15.2",
                "strimzi-kafka-operator": "0.45.0",
            },
            oci_base="oci://ghcr.io/hyperi-io/helm-charts",
            run_helm_dep_update=False,
            run_helm_lint=False,
        )
    assert "missing" in str(ei.value).lower() or "not found" in str(ei.value).lower()
