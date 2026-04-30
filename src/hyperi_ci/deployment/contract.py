# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/contract.py
# Purpose:   Pydantic mirror of hyperi-rustlib::deployment::DeploymentContract
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Pydantic data model that mirrors rustlib's `DeploymentContract`.

Field-for-field parity with `hyperi-rustlib/src/deployment/contract.rs`,
`keda.rs`, and `native_deps.rs`. Any divergence here causes parity-test
failures against fixtures emitted by rustlib's serde_json.

Naming matches rustlib's serde-default field names. snake_case throughout
because rustlib uses serde without `rename_all` overrides on these types.

Schema versioning:
  schema_version is stamped on every emitted JSON. Consumers (this module,
  CI stages, dfe-control-plane) fail fast when the contract declares a
  version higher than `MAX_SUPPORTED_SCHEMA_VERSION` (config/defaults.yaml).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Defaults — must match rustlib's `default_*()` functions in contract.rs,
# keda.rs, and native_deps.rs. Bumping any of these requires a coordinated
# rustlib + pylib + hyperi-ci release.
DEFAULT_SCHEMA_VERSION = 2
DEFAULT_IMAGE_REGISTRY = "ghcr.io/hyperi-io"
DEFAULT_BASE_IMAGE = "ubuntu:24.04"
DEFAULT_VENDOR = "HYPERI PTY LIMITED"
DEFAULT_LICENSE = "FSL-1.1-ALv2"
DEFAULT_PROTOCOL = "TCP"

# Highest contract schema version this hyperi-ci can consume. Bumped in
# lockstep with rustlib + pylib on shape changes. Mirrored in
# config/defaults.yaml under `deployment.max_supported_schema_version`
# for operator visibility — this constant is the strict gate.
MAX_SUPPORTED_SCHEMA_VERSION = 2


class _StrictModel(BaseModel):
    """Base for all contract types — frozen, extra=forbid for parity safety.

    `extra=forbid` catches schema drift early: a JSON contract that has a
    field hyperi-ci doesn't know about would silently ignore it without
    this. Frozen mirrors rustlib's #[derive(Clone)] semantics — contracts
    are read-once, not mutated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ImageProfile(StrEnum):
    """Container image profile — production (minimal) or development (debug tools).

    Mirrors `hyperi-rustlib::deployment::contract::ImageProfile` with
    ``#[serde(rename_all = "lowercase")]`` — so JSON values are
    ``"production"`` and ``"development"``.
    """

    PRODUCTION = "production"
    DEVELOPMENT = "development"


class HealthContract(_StrictModel):
    """Health probe endpoint paths.

    Mirrors `hyperi-rustlib::deployment::contract::HealthContract`.
    """

    liveness_path: str = "/healthz"
    readiness_path: str = "/readyz"
    metrics_path: str = "/metrics"


class PortContract(_StrictModel):
    """Additional container port beyond the metrics port.

    Mirrors `hyperi-rustlib::deployment::contract::PortContract`.
    """

    name: str
    port: int = Field(ge=1, le=65535)
    protocol: str = DEFAULT_PROTOCOL


class SecretEnvContract(_StrictModel):
    """A single environment variable sourced from a K8s Secret.

    Mirrors `hyperi-rustlib::deployment::contract::SecretEnvContract`.
    """

    env_var: str
    key_name: str
    secret_key: str


class SecretGroupContract(_StrictModel):
    """A group of secrets from the same K8s Secret.

    Mirrors `hyperi-rustlib::deployment::contract::SecretGroupContract`.
    """

    group_name: str
    env_vars: list[SecretEnvContract]


class OciLabels(_StrictModel):
    """OCI image labels for the container.

    Static labels are set from the contract. Dynamic labels (source,
    revision, version, created) are injected by CI at build time via
    --build-arg.

    Mirrors `hyperi-rustlib::deployment::contract::OciLabels`.
    """

    title: str = ""
    description: str = ""
    vendor: str = DEFAULT_VENDOR
    licenses: str = DEFAULT_LICENSE


class AptRepoContract(_StrictModel):
    """A custom APT repository (e.g., Confluent for librdkafka).

    Mirrors `hyperi-rustlib::deployment::native_deps::AptRepoContract`.
    """

    key_url: str
    keyring: str
    url: str
    codename: str = ""
    packages: list[str]


class NativeDepsContract(_StrictModel):
    """Runtime native dependencies for a container image.

    Mirrors `hyperi-rustlib::deployment::native_deps::NativeDepsContract`.
    """

    apt_repos: list[AptRepoContract] = Field(default_factory=list)
    apt_packages: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """Mirror Rust `NativeDepsContract::is_empty`."""
        return not self.apt_repos and not self.apt_packages


class KedaContract(_StrictModel):
    """KEDA contract points validated against Helm values.yaml.

    Mirrors `hyperi-rustlib::deployment::keda::KedaContract`. Defaults
    derived from `KedaConfig::default()` in rustlib.
    """

    min_replicas: int = Field(default=1, ge=0)
    max_replicas: int = Field(default=10, ge=1)
    polling_interval: int = Field(default=15, ge=1)
    cooldown_period: int = Field(default=300, ge=0)
    kafka_lag_threshold: int = Field(default=1000, ge=0)
    activation_lag_threshold: int = Field(default=0, ge=0)
    cpu_enabled: bool = True
    cpu_threshold: int = Field(default=80, ge=1, le=100)


class DeploymentContract(_StrictModel):
    """Deployment-facing contract derived from the app config cascade.

    Apps build this from their `Config::default()` (Rust) or equivalent
    (Python). Generation functions create deployment artefacts (Dockerfile,
    Helm chart, Compose fragment, ArgoCD Application, container manifest)
    from the same source.

    Mirrors `hyperi-rustlib::deployment::contract::DeploymentContract`.
    Field order, defaults, and serde behaviour MUST track rustlib exactly
    — the parity tests assert byte-identical JSON output for shared
    fixtures.
    """

    schema_version: int = DEFAULT_SCHEMA_VERSION
    app_name: str
    binary_name: str = ""
    description: str = ""
    metrics_port: int = Field(ge=1, le=65535)
    health: HealthContract
    env_prefix: str
    metric_prefix: str
    config_mount_path: str
    image_registry: str = DEFAULT_IMAGE_REGISTRY
    extra_ports: list[PortContract] = Field(default_factory=list)
    entrypoint_args: list[str] = Field(default_factory=list)
    secrets: list[SecretGroupContract] = Field(default_factory=list)
    default_config: dict[str, Any] | None = None
    depends_on: list[str] = Field(default_factory=list)
    keda: KedaContract | None = None
    base_image: str = DEFAULT_BASE_IMAGE
    native_deps: NativeDepsContract = Field(default_factory=NativeDepsContract)
    image_profile: ImageProfile = ImageProfile.PRODUCTION
    oci_labels: OciLabels = Field(default_factory=OciLabels)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: int) -> int:
        """Reject contracts that declare a newer schema than this hyperi-ci supports.

        The producer side stamps `schema_version` on every emitted JSON.
        On the consumer side, hyperi-ci loads the JSON and parses it
        through this model — the validator fires before any artefact
        templating begins, so a newer-schema contract aborts the whole
        emit-artefacts run with a clear error rather than producing
        Dockerfiles that don't match what the producer intended.

        Lower-than-current is fine — newer hyperi-ci is expected to read
        older contracts (forward compatibility within the major version).

        Note: this constant must be bumped in lockstep with the
        `deployment.max_supported_schema_version` key in
        `config/defaults.yaml` and in rustlib + pylib at every coordinated
        release. The yaml entry is for operator visibility only — this
        constant is the strict gate.
        """
        if v > MAX_SUPPORTED_SCHEMA_VERSION:
            msg = (
                f"contract declares schema_version={v} but this "
                f"hyperi-ci supports up to {MAX_SUPPORTED_SCHEMA_VERSION}; "
                "upgrade hyperi-ci"
            )
            raise ValueError(msg)
        if v < 1:
            msg = f"schema_version must be >= 1, got {v}"
            raise ValueError(msg)
        return v

    def binary(self) -> str:
        """Effective binary name — falls back to app_name if binary_name unset.

        Mirrors `DeploymentContract::binary` in rustlib.
        """
        return self.binary_name or self.app_name

    def config_filename(self) -> str:
        """Config file name from the mount path (e.g., ``loader.yaml``).

        Mirrors `DeploymentContract::config_filename` in rustlib.
        """
        if "/" not in self.config_mount_path:
            return self.config_mount_path or "config.yaml"
        return self.config_mount_path.rsplit("/", 1)[1] or "config.yaml"

    def config_dir(self) -> str:
        """Config mount directory (e.g., ``/etc/dfe``).

        Mirrors `DeploymentContract::config_dir` in rustlib.
        """
        if "/" not in self.config_mount_path:
            return "/etc"
        head = self.config_mount_path.rsplit("/", 1)[0]
        return head or "/etc"
