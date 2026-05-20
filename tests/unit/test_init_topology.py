"""Tests for init_gitops.init_topology()."""

from __future__ import annotations

import pytest
import yaml

from hyperi_ci.init_gitops import GitopsInitError, init_topology


def test_init_topology_creates_skeleton(tmp_path):
    rc = init_topology(
        gitops_root=tmp_path,
        name="my-topo",
        apps=["dfe-loader", "dfe-receiver"],
    )

    assert rc == 0
    topo_dir = tmp_path / "topologies" / "my-topo"
    assert topo_dir.is_dir()
    assert (topo_dir / "topology.yaml").exists()
    assert (topo_dir / "values.yaml").exists()
    assert (topo_dir / "glue").is_dir()
    assert (topo_dir / "README.md").exists()


def test_init_topology_seeds_apps_in_topology_yaml(tmp_path):
    init_topology(
        gitops_root=tmp_path,
        name="my-topo",
        apps=["dfe-loader", "dfe-archiver"],
    )

    topo_yaml = (tmp_path / "topologies" / "my-topo" / "topology.yaml").read_text(
        encoding="utf-8"
    )
    parsed = yaml.safe_load(topo_yaml)
    app_names = [a["name"] for a in parsed["spec"]["apps"]]

    assert "dfe-loader" in app_names
    assert "dfe-archiver" in app_names
    assert parsed["metadata"]["name"] == "my-topo"


def test_init_topology_rejects_existing_dir(tmp_path):
    (tmp_path / "topologies" / "my-topo").mkdir(parents=True)

    with pytest.raises(GitopsInitError):
        init_topology(
            gitops_root=tmp_path,
            name="my-topo",
            apps=["dfe-loader"],
        )


def test_init_topology_rejects_invalid_name(tmp_path):
    with pytest.raises(GitopsInitError) as exc_info:
        init_topology(
            gitops_root=tmp_path,
            name="My-Topo",
            apps=["dfe-loader"],
        )

    assert "lowercase" in str(exc_info.value).lower()
