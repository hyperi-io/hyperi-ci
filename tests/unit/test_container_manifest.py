# Project:   HyperI CI
# File:      tests/unit/test_container_manifest.py
# Purpose:   Tests for container manifest parser
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json

import pytest

from hyperi_ci.container.manifest import ContainerManifest, load_manifest


def test_load_manifest_from_dict():
    data = {
        "base_image": "debian:bookworm-slim",
        "binary_name": "dfe-loader",
        "runtime_packages": ["ca-certificates", "librdkafka1", "libzstd1"],
        "expose_ports": [8080, 9090],
        "health_check": {
            "path": "/health/live",
            "port": 8080,
            "interval": 30,
            "timeout": 3,
            "start_period": 10,
            "retries": 3,
        },
        "user": {"name": "appuser", "uid": 1000},
        "entrypoint": ["dfe-loader"],
        "cmd": ["run"],
        "env": {"RUST_LOG": "info"},
        "labels": {"io.hyperi.app": "dfe-loader"},
    }
    manifest = ContainerManifest.from_dict(data)
    assert manifest.base_image == "debian:bookworm-slim"
    assert manifest.binary_name == "dfe-loader"
    assert "librdkafka1" in manifest.runtime_packages
    assert manifest.expose_ports == [8080, 9090]
    assert manifest.health_check["path"] == "/health/live"
    assert manifest.user["uid"] == 1000
    assert manifest.entrypoint == ["dfe-loader"]


def test_load_manifest_from_file(tmp_path):
    data = {
        "base_image": "debian:bookworm-slim",
        "binary_name": "dfe-loader",
        "runtime_packages": [],
        "expose_ports": [8080],
        "health_check": {"path": "/healthz", "port": 8080},
        "user": {"name": "appuser", "uid": 1000},
        "entrypoint": ["dfe-loader"],
        "cmd": [],
    }
    manifest_file = tmp_path / "container-manifest.json"
    manifest_file.write_text(json.dumps(data))
    manifest = load_manifest(manifest_file)
    assert manifest.binary_name == "dfe-loader"


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path / "nonexistent.json")


def test_manifest_missing_required_field():
    with pytest.raises(ValueError, match="binary_name"):
        ContainerManifest.from_dict({"base_image": "debian:slim"})


def test_manifest_defaults():
    data = {"base_image": "debian:slim", "binary_name": "myapp"}
    manifest = ContainerManifest.from_dict(data)
    assert manifest.runtime_packages == []
    assert manifest.expose_ports == []
    assert manifest.user == {"name": "appuser", "uid": 1000}
    assert manifest.entrypoint == []
    assert manifest.cmd == []
    assert manifest.env == {}
    assert manifest.labels == {}
