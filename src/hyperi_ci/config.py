# Project:   HyperI CI
# File:      src/hyperi_ci/config.py
# Purpose:   Typed configuration schema and loader
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Configuration schema, loading, and validation for HyperI CI.

Cascade priority (highest wins):
  CLI flags -> ENV vars (HYPERCI_*) -> .hyperi-ci.yaml -> defaults.yaml -> hardcoded
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).resolve().parent / "config"

# Re-exported for callers that just want the constant without going through
# the full CIConfig load (e.g. quality-stage drift checks). Authoritative
# value is defined alongside the Pydantic model that uses it as a field
# validator. Mirrored as `deployment.max_supported_schema_version` in
# defaults.yaml for operator visibility.
from hyperi_ci.deployment.contract import (  # noqa: E402,F401
    MAX_SUPPORTED_SCHEMA_VERSION,
)


@dataclass
class OrgConfig:
    """Organisation-specific configuration loaded from config/org.yaml."""

    github_org: str = "hyperi-io"
    github_base_url: str = "https://github.com/hyperi-io"
    jfrog_domain: str = "hypersec.jfrog.io"
    jfrog_org_prefix: str = "hyperi"
    ghcr_registry: str = "ghcr.io"
    ghcr_org: str = "hyperi-io"

    # Derived JFrog URLs
    artifactory_base_url: str = ""
    pypi_url: str = ""
    pypi_publish_url: str = ""
    npm_url: str = ""
    cargo_url: str = ""
    cargo_publish_url: str = ""
    binary_url: str = ""
    docker_registry: str = ""
    helm_url: str = ""
    ghcr_charts_url: str = ""

    def __post_init__(self) -> None:
        """Derive URLs from base config."""
        base = f"https://{self.jfrog_domain}/artifactory"
        pfx = self.jfrog_org_prefix
        if not self.artifactory_base_url:
            self.artifactory_base_url = base
        if not self.pypi_url:
            self.pypi_url = f"{base}/api/pypi/{pfx}-pypi/simple"
        if not self.pypi_publish_url:
            self.pypi_publish_url = f"{base}/api/pypi/{pfx}-pypi-local"
        if not self.npm_url:
            self.npm_url = f"{base}/api/npm/{pfx}-npm"
        if not self.cargo_url:
            self.cargo_url = f"{base}/api/cargo/{pfx}-cargo-virtual"
        if not self.cargo_publish_url:
            self.cargo_publish_url = f"{base}/api/cargo/{pfx}-cargo-local"
        if not self.binary_url:
            self.binary_url = f"{base}/{pfx}-binaries"
        if not self.docker_registry:
            self.docker_registry = f"{self.jfrog_domain}/{pfx}-docker-local"
        if not self.helm_url:
            self.helm_url = f"oci://{self.jfrog_domain}/{pfx}-helm-local"
        if not self.ghcr_charts_url:
            self.ghcr_charts_url = f"oci://{self.ghcr_registry}/{self.ghcr_org}/charts"


@dataclass
class CIConfig:
    """Full CI configuration after merging all sources."""

    language: str = "none"
    ci_min_python_version: str = "3.9"
    publish_target: str = "internal"

    # Raw merged dict for accessing nested language-specific config
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value by dot-notation key."""
        value: Any = self._raw
        for k in key.split("."):
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def publish_destinations(self) -> list[dict[str, str]]:
        """Return list of destination maps to publish to based on target.

        Returns one or two dicts from destinations_internal / destinations_oss
        depending on publish_target being 'internal', 'oss', or 'both'.
        """
        result: list[dict[str, str]] = []
        if self.publish_target in ("internal", "both"):
            dest = self.get("publish.destinations_internal", {})
            if isinstance(dest, dict):
                result.append(dest)
        if self.publish_target in ("oss", "both"):
            dest = self.get("publish.destinations_oss", {})
            if isinstance(dest, dict):
                result.append(dest)
        return result

    def destination_for(self, artifact_type: str) -> list[str]:
        """Get publish destination(s) for a specific artifact type.

        Args:
            artifact_type: One of python, npm, cargo, container, helm, binaries, go.

        Returns:
            List of destination identifiers (e.g. ['jfrog-pypi', 'pypi']).

        """
        return [
            dest[artifact_type]
            for dest in self.publish_destinations()
            if artifact_type in dest
        ]


def _merge_deep(base: dict, override: dict) -> dict:
    """Deep merge override into base dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_deep(result[key], value)
        else:
            result[key] = value
    return result


def _parse_env_value(value: str) -> Any:
    """Parse environment variable string to appropriate Python type."""
    if value.lower() in ("true", "yes", "1"):
        return True
    if value.lower() in ("false", "no", "0"):
        return False
    if value.isdigit():
        return int(value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _set_nested(config: dict, path: list[str], value: Any) -> None:
    """Set a nested configuration value by path segments."""
    if len(path) == 1:
        config[path[0]] = value
    else:
        if path[0] not in config:
            config[path[0]] = {}
        _set_nested(config[path[0]], path[1:], value)


_config_cache: CIConfig | None = None
_org_cache: OrgConfig | None = None


def load_org_config(*, reload: bool = False) -> OrgConfig:
    """Load organisation config from config/org.yaml."""
    global _org_cache
    if _org_cache is not None and not reload:
        return _org_cache

    org_file = _CONFIG_DIR / "org.yaml"
    raw: dict[str, Any] = {}
    if org_file.exists():
        with open(org_file) as f:
            loaded = yaml.safe_load(f)
            if loaded:
                raw = loaded

    github = raw.get("github", {})
    jfrog = raw.get("jfrog", {})
    ghcr = raw.get("ghcr", {})

    _org_cache = OrgConfig(
        github_org=os.environ.get("GITHUB_ORG", github.get("org", "hyperi-io")),
        github_base_url=github.get("base_url", "https://github.com/hyperi-io"),
        jfrog_domain=os.environ.get(
            "JFROG_DOMAIN",
            jfrog.get("domain", "hypersec.jfrog.io"),
        ),
        jfrog_org_prefix=jfrog.get("org_prefix", "hyperi"),
        ghcr_registry=ghcr.get("registry", "ghcr.io"),
        ghcr_org=ghcr.get("org", "hyperi-io"),
    )
    return _org_cache


def load_config(
    *,
    reload: bool = False,
    project_dir: Path | None = None,
) -> CIConfig:
    """Load and merge CI configuration from all sources.

    Cascade (highest last):
      1. config/defaults.yaml (package defaults)
      2. .hyperi-ci.yaml (project override)
      3. HYPERCI_* environment variables

    Args:
        reload: Force re-read from files.
        project_dir: Project root to search for .hyperi-ci.yaml. Defaults to cwd.

    Returns:
        Merged CIConfig instance.

    """
    global _config_cache
    if _config_cache is not None and not reload:
        return _config_cache

    config: dict[str, Any] = {}
    project_dir = project_dir or Path.cwd()

    # Load package defaults
    defaults_file = _CONFIG_DIR / "defaults.yaml"
    if defaults_file.exists():
        with open(defaults_file) as f:
            loaded = yaml.safe_load(f)
            if loaded:
                config = loaded

    # Load project config
    for name in (
        ".hyperi-ci.yaml",
        ".hyperi-ci.yml",
        ".hypersec-ci.yaml",
        ".hypersec-ci.yml",
    ):
        config_file = project_dir / name
        if config_file.exists():
            with open(config_file) as f:
                loaded = yaml.safe_load(f)
                if loaded:
                    config = _merge_deep(config, loaded)
            break

    # Apply HYPERCI_* env overrides
    for key, value in os.environ.items():
        if key.startswith("HYPERCI_"):
            path = key[8:].lower().split("_")
            _set_nested(config, path, _parse_env_value(value))

    publish = config.get("publish", {})
    publish_target = (
        publish.get("target", "internal") if isinstance(publish, dict) else "internal"
    )

    _config_cache = CIConfig(
        language=config.get("language", "none"),
        ci_min_python_version=config.get("ci_min_python_version", "3.9"),
        publish_target=publish_target,
        _raw=config,
    )
    return _config_cache
