# Project:   HyperI CI
# File:      tests/unit/test_container_compose.py
# Purpose:   Tests for contract-driven Dockerfile composition
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from hyperi_ci.container.compose import (
    _rust_channel_switch,
    _rust_slim_tag,
    compose_contract_dockerfile,
)
from hyperi_ci.container.manifest import ContainerManifest


class TestRustSlimTag:
    """Every generated `FROM rust:<tag>` must be a tag that EXISTS.

    Docker Hub publishes no tag for any rustup CHANNEL - `rust:stable-slim`,
    `rust:nightly-slim` and `rust:beta-slim` all 404 (verified with
    `docker manifest inspect`). `_detect_rust_version()` returns the channel
    from rust-toolchain.toml verbatim and falls back to "stable" when the file
    is absent, so the unresolvable-image path was the DEFAULT one.
    """

    @pytest.mark.parametrize(
        "channel", ["stable", "beta", "nightly", "nightly-2026-01-01", "", "latest"]
    )
    def test_channels_map_to_the_stable_slim_image(self, channel: str) -> None:
        assert _rust_slim_tag(channel) == "slim"

    @pytest.mark.parametrize(
        ("version", "want"), [("1.90", "1.90-slim"), ("1", "1-slim")]
    )
    def test_concrete_versions_keep_their_tag(self, version: str, want: str) -> None:
        assert _rust_slim_tag(version) == want

    @pytest.mark.parametrize("channel", ["stable", "1.90", "", "latest"])
    def test_no_rustup_switch_when_stable(self, channel: str) -> None:
        assert _rust_channel_switch(channel) == ""

    @pytest.mark.parametrize("channel", ["beta", "nightly", "nightly-2026-01-01"])
    def test_non_stable_channels_switch_with_rustup(self, channel: str) -> None:
        # The image is stable, so without this the build would silently compile
        # on the WRONG toolchain - a wrong build, not a failed one.
        line = _rust_channel_switch(channel)
        assert f"rustup default {channel}" in line


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


def test_compose_user_uid_1000_removes_existing_ubuntu_user():
    """Ubuntu 24.04 ships with `ubuntu` at UID 1000; bare useradd would
    fail with 'UID is not unique' (exit 4). The composer must `userdel
    -r ubuntu` first when the requested UID is 1000.
    """
    result = compose_contract_dockerfile(_sample_manifest(), rust_version="1.87")
    assert "userdel -r ubuntu" in result
    assert "useradd --create-home --uid 1000 appuser" in result


def test_compose_user_non_default_uid_skips_userdel():
    """Non-default UIDs (e.g. 65534/nobody) don't need the ubuntu workaround."""
    manifest = ContainerManifest(
        base_image="debian:bookworm-slim",
        binary_name="simple-app",
        expose_ports=[8080],
        entrypoint=["simple-app"],
        user={"name": "nobody", "uid": 65534},
    )
    result = compose_contract_dockerfile(manifest, rust_version="1.87")
    assert "userdel" not in result
    assert "useradd --create-home --uid 65534 nobody" in result


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
    assert "FROM debian:bookworm-slim AS runtime" in result
    assert "COPY --from=builder" in result
    # No declared packages -> the RUNTIME image installs nothing. Scoped to the
    # runtime stage rather than the whole file: the chef BUILD stage legitimately
    # apt-gets curl/xz to fetch the prebuilt cargo-chef binary, and that says
    # nothing about what ships in the final image.
    runtime_stage = result.split("AS runtime", 1)[1]
    assert "apt-get" not in runtime_stage
