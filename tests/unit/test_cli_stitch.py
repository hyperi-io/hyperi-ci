# Project:   HyperI CI
# File:      tests/unit/test_cli_stitch.py
# Purpose:   CLI tests for `hyperi-ci stitch`
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""CLI tests for `hyperi-ci stitch`."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from hyperi_ci.cli import app

runner = CliRunner()


def _seed_topology(topo_dir: Path) -> None:
    topo_dir.mkdir(parents=True)
    (topo_dir / "topology.yaml").write_text(
        """\
apiVersion: hyperi.io/v1
kind: DeploymentTopology
metadata:
  name: minimal
spec:
  umbrella:
    name: hyperi-deployment-minimal
    description: minimal
    appVersion: "1.0"
  apps:
    - name: dfe-loader
      version: "^1.18"
""",
        encoding="utf-8",
    )


def test_stitch_help_exits_zero():
    result = runner.invoke(app, ["stitch", "--help"])
    assert result.exit_code == 0
    assert "stitch" in result.stdout.lower()


def test_stitch_invalid_topology_dir_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["stitch", str(tmp_path / "does-not-exist")])
    assert result.exit_code != 0


def test_stitch_writes_output(tmp_path, monkeypatch):
    topo_dir = tmp_path / "default"
    _seed_topology(topo_dir)
    out_dir = tmp_path / "stitched"

    # Stub OCI resolution so we don't hit the registry
    from hyperi_ci.deployment.topology import resolve

    def _stub_fetch(registry, charts):
        return {c: ["1.18.3"] for c in charts}

    monkeypatch.setattr(resolve, "_fetch_available", _stub_fetch)

    result = runner.invoke(
        app,
        [
            "stitch",
            str(topo_dir),
            "--output-dir",
            str(out_dir),
            "--skip-helm-dep-update",
            "--skip-helm-lint",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (out_dir / "Chart.yaml").exists()
    chart = yaml.safe_load((out_dir / "Chart.yaml").read_text())
    assert chart["name"] == "hyperi-deployment-minimal"
    assert chart["dependencies"][0]["version"] == "1.18.3"
