# Project:   HyperI CI
# File:      tests/unit/deployment/test_contract.py
# Purpose:   Unit tests for the Pydantic deployment contract model
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.deployment.contract`."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hyperi_ci.deployment import (
    AptRepoContract,
    DeploymentContract,
    HealthContract,
    ImageProfile,
    KedaContract,
    NativeDepsContract,
    OciLabels,
    PortContract,
    SecretEnvContract,
    SecretGroupContract,
)
from hyperi_ci.deployment.contract import (
    DEFAULT_BASE_IMAGE,
    DEFAULT_IMAGE_REGISTRY,
    DEFAULT_LICENSE,
    DEFAULT_PROTOCOL,
    DEFAULT_SCHEMA_VERSION,
    DEFAULT_VENDOR,
    MAX_SUPPORTED_SCHEMA_VERSION,
)


def _minimal_contract_dict() -> dict:
    """Return the smallest contract dict that parses (every required field set).

    Used as a fresh starting point in every test that mutates one field —
    avoids tests sharing state via a module-level fixture dict.
    """
    return {
        "app_name": "test-app",
        "metrics_port": 9090,
        "health": {
            "liveness_path": "/healthz",
            "readiness_path": "/readyz",
            "metrics_path": "/metrics",
        },
        "env_prefix": "TEST_APP",
        "metric_prefix": "test",
        "config_mount_path": "/etc/test/config.yaml",
    }


class TestHealthContract:
    """`HealthContract` defaults match rustlib's Default impl."""

    def test_defaults(self) -> None:
        h = HealthContract()
        assert h.liveness_path == "/healthz"
        assert h.readiness_path == "/readyz"
        assert h.metrics_path == "/metrics"

    def test_custom_paths(self) -> None:
        h = HealthContract(
            liveness_path="/live",
            readiness_path="/ready",
            metrics_path="/m",
        )
        assert h.liveness_path == "/live"


class TestPortContract:
    """`PortContract` field validation."""

    def test_minimal(self) -> None:
        p = PortContract(name="http", port=8080)
        assert p.protocol == DEFAULT_PROTOCOL

    def test_port_out_of_range_low(self) -> None:
        with pytest.raises(ValidationError):
            PortContract(name="http", port=0)

    def test_port_out_of_range_high(self) -> None:
        with pytest.raises(ValidationError):
            PortContract(name="http", port=65536)


class TestSecretGroup:
    """`SecretGroupContract` requires a non-empty env_vars list at parse-time
    only when caller passes the wrong shape — empty list is allowed by rustlib."""

    def test_empty_envs_allowed(self) -> None:
        # rustlib's Vec<SecretEnvContract> can be empty; mirror that.
        g = SecretGroupContract(group_name="kafka", env_vars=[])
        assert g.env_vars == []

    def test_with_envs(self) -> None:
        g = SecretGroupContract(
            group_name="kafka",
            env_vars=[
                SecretEnvContract(
                    env_var="DFE_LOADER__KAFKA__PASSWORD",
                    key_name="password",
                    secret_key="kafka-password",
                ),
            ],
        )
        assert g.env_vars[0].env_var == "DFE_LOADER__KAFKA__PASSWORD"


class TestOciLabels:
    """`OciLabels` defaults match rustlib's Default impl."""

    def test_defaults(self) -> None:
        labels = OciLabels()
        assert labels.title == ""
        assert labels.description == ""
        assert labels.vendor == DEFAULT_VENDOR
        assert labels.licenses == DEFAULT_LICENSE


class TestNativeDeps:
    """`NativeDepsContract` defaults to empty; `is_empty()` mirrors rustlib."""

    def test_empty_default(self) -> None:
        deps = NativeDepsContract()
        assert deps.is_empty()
        assert deps.apt_repos == []
        assert deps.apt_packages == []

    def test_is_empty_with_packages(self) -> None:
        deps = NativeDepsContract(apt_packages=["libssl3"])
        assert not deps.is_empty()

    def test_is_empty_with_repos(self) -> None:
        deps = NativeDepsContract(
            apt_repos=[
                AptRepoContract(
                    key_url="https://example.com/key",
                    keyring="/usr/share/keyrings/test.gpg",
                    url="https://example.com/repo",
                    packages=["foo"],
                ),
            ],
        )
        assert not deps.is_empty()


class TestKedaContract:
    """`KedaContract` defaults match `KedaConfig::default()` from rustlib."""

    def test_defaults(self) -> None:
        keda = KedaContract()
        assert keda.min_replicas == 1
        assert keda.max_replicas == 10
        assert keda.polling_interval == 15
        assert keda.cooldown_period == 300
        assert keda.kafka_lag_threshold == 1000
        assert keda.activation_lag_threshold == 0
        assert keda.cpu_enabled is True
        assert keda.cpu_threshold == 80


class TestDeploymentContract:
    """`DeploymentContract` parsing, defaults, helpers, and serde behaviour."""

    def test_minimal_parse(self) -> None:
        contract = DeploymentContract(**_minimal_contract_dict())
        assert contract.app_name == "test-app"
        assert contract.metrics_port == 9090
        assert contract.schema_version == DEFAULT_SCHEMA_VERSION
        assert contract.image_registry == DEFAULT_IMAGE_REGISTRY
        assert contract.base_image == DEFAULT_BASE_IMAGE
        assert contract.image_profile == ImageProfile.PRODUCTION
        assert contract.keda is None
        assert contract.native_deps.is_empty()

    def test_binary_falls_back_to_app_name(self) -> None:
        contract = DeploymentContract(**_minimal_contract_dict())
        assert contract.binary() == "test-app"

    def test_binary_explicit(self) -> None:
        d = _minimal_contract_dict()
        d["binary_name"] = "custom-bin"
        contract = DeploymentContract(**d)
        assert contract.binary() == "custom-bin"

    def test_config_filename_and_dir(self) -> None:
        d = _minimal_contract_dict()
        d["config_mount_path"] = "/etc/dfe/loader.yaml"
        contract = DeploymentContract(**d)
        assert contract.config_filename() == "loader.yaml"
        assert contract.config_dir() == "/etc/dfe"

    def test_config_filename_no_directory(self) -> None:
        d = _minimal_contract_dict()
        d["config_mount_path"] = "config.yaml"
        contract = DeploymentContract(**d)
        assert contract.config_filename() == "config.yaml"
        assert contract.config_dir() == "/etc"

    def test_image_profile_serialises_lowercase(self) -> None:
        # rustlib uses #[serde(rename_all = "lowercase")]; mirror that.
        d = _minimal_contract_dict()
        d["image_profile"] = "development"
        contract = DeploymentContract(**d)
        assert contract.image_profile == ImageProfile.DEVELOPMENT

    def test_image_profile_invalid_value(self) -> None:
        d = _minimal_contract_dict()
        d["image_profile"] = "Production"  # uppercase = bad
        with pytest.raises(ValidationError):
            DeploymentContract(**d)

    def test_extra_field_forbidden(self) -> None:
        d = _minimal_contract_dict()
        d["unknown_field"] = "boom"
        with pytest.raises(ValidationError) as exc:
            DeploymentContract(**d)
        assert "unknown_field" in str(exc.value).lower()

    def test_full_roundtrip_via_json(self) -> None:
        # Serialise the model to JSON, parse back, assert equality.
        # This is the property parity tests will eventually verify against
        # rustlib's serde_json output as well.
        d = _minimal_contract_dict()
        d["binary_name"] = "loader"
        d["description"] = "Test loader"
        d["extra_ports"] = [{"name": "http", "port": 8080, "protocol": "TCP"}]
        d["depends_on"] = ["kafka"]
        d["keda"] = {"max_replicas": 20}
        d["native_deps"] = {
            "apt_packages": ["libssl3", "zlib1g"],
            "apt_repos": [],
        }

        original = DeploymentContract(**d)
        as_json = original.model_dump_json()
        roundtripped = DeploymentContract(**json.loads(as_json))
        assert roundtripped == original
        assert roundtripped.keda is not None
        assert roundtripped.keda.max_replicas == 20

    def test_missing_required_field(self) -> None:
        d = _minimal_contract_dict()
        del d["app_name"]
        with pytest.raises(ValidationError) as exc:
            DeploymentContract(**d)
        assert "app_name" in str(exc.value)


class TestSchemaVersion:
    """Schema-version field validator enforces the supported range."""

    def test_default_is_supported(self) -> None:
        contract = DeploymentContract(**_minimal_contract_dict())
        assert contract.schema_version == DEFAULT_SCHEMA_VERSION
        assert contract.schema_version <= MAX_SUPPORTED_SCHEMA_VERSION

    def test_explicit_at_max_supported_ok(self) -> None:
        d = _minimal_contract_dict()
        d["schema_version"] = MAX_SUPPORTED_SCHEMA_VERSION
        contract = DeploymentContract(**d)
        assert contract.schema_version == MAX_SUPPORTED_SCHEMA_VERSION

    def test_one_above_max_rejected(self) -> None:
        d = _minimal_contract_dict()
        d["schema_version"] = MAX_SUPPORTED_SCHEMA_VERSION + 1
        with pytest.raises(ValidationError) as exc:
            DeploymentContract(**d)
        msg = str(exc.value)
        assert "schema_version" in msg
        assert "upgrade hyperi-ci" in msg

    def test_far_future_version_rejected(self) -> None:
        d = _minimal_contract_dict()
        d["schema_version"] = 99
        with pytest.raises(ValidationError):
            DeploymentContract(**d)

    def test_zero_rejected(self) -> None:
        d = _minimal_contract_dict()
        d["schema_version"] = 0
        with pytest.raises(ValidationError):
            DeploymentContract(**d)

    def test_negative_rejected(self) -> None:
        d = _minimal_contract_dict()
        d["schema_version"] = -1
        with pytest.raises(ValidationError):
            DeploymentContract(**d)
