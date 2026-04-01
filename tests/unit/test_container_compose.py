# Project:   HyperI CI
# File:      tests/unit/test_container_compose.py
# Purpose:   Tests for contract-driven Dockerfile composition
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from hyperi_ci.container.compose import compose_contract_dockerfile
from hyperi_ci.container.manifest import ContainerManifest


def _sample_manifest() -> ContainerManifest:
    return ContainerManifest(
        base_image="debian:bookworm-slim",
        binary_name="dfe-loader",
        runtime_packages=["ca-certificates", "librdkafka1"],
        expose_ports=[8080, 9090],
        health_check={
            "path": "/health/live",
            "port": 8080,
            "interval": 30,
            "timeout": 3,
            "start_period": 10,
            "retries": 3,
        },
        user={"name": "appuser", "uid": 1000},
        entrypoint=["dfe-loader"],
        cmd=["run"],
        env={"RUST_LOG": "info"},
    )


def test_compose_has_chef_stages():
    result = compose_contract_dockerfile(_sample_manifest(), rust_version="1.87")
    assert "FROM rust:1.87-slim AS chef" in result
    assert "cargo chef prepare" in result
    assert "cargo chef cook --release" in result
    assert "cargo build --release --bin dfe-loader" in result


def test_compose_has_runtime_stage():
    result = compose_contract_dockerfile(_sample_manifest(), rust_version="1.87")
    assert "FROM debian:bookworm-slim AS runtime" in result
    assert "ca-certificates" in result
    assert "librdkafka1" in result
    assert (
        "COPY --from=builder /app/target/release/dfe-loader /usr/local/bin/dfe-loader"
        in result
    )


def test_compose_has_healthcheck():
    result = compose_contract_dockerfile(_sample_manifest(), rust_version="1.87")
    assert "HEALTHCHECK" in result
    assert "/health/live" in result
    assert "EXPOSE 8080" in result
    assert "EXPOSE 9090" in result


def test_compose_has_user():
    result = compose_contract_dockerfile(_sample_manifest(), rust_version="1.87")
    assert "useradd" in result
    assert "USER appuser" in result


def test_compose_has_env():
    result = compose_contract_dockerfile(_sample_manifest(), rust_version="1.87")
    assert 'ENV RUST_LOG="info"' in result


def test_compose_has_entrypoint():
    result = compose_contract_dockerfile(_sample_manifest(), rust_version="1.87")
    assert 'ENTRYPOINT ["dfe-loader"]' in result
    assert 'CMD ["run"]' in result


def test_compose_no_packages():
    manifest = ContainerManifest(
        base_image="debian:bookworm-slim",
        binary_name="simple-app",
        expose_ports=[8080],
        entrypoint=["simple-app"],
    )
    result = compose_contract_dockerfile(manifest, rust_version="1.87")
    assert "apt-get" not in result
    assert "FROM debian:bookworm-slim AS runtime" in result
    assert "COPY --from=builder" in result
