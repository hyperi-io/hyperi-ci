# Project:   HyperI CI
# File:      src/hyperi_ci/container/manifest.py
# Purpose:   Parse container-manifest.json from rustlib deployment contract
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Parse container-manifest.json emitted by rustlib deployment contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REQUIRED_FIELDS = ["base_image", "binary_name"]


@dataclass
class ContainerManifest:
    """Parsed container manifest from a rustlib deployment contract."""

    base_image: str
    binary_name: str
    runtime_packages: list[str] = field(default_factory=list)
    expose_ports: list[int] = field(default_factory=list)
    health_check: dict[str, Any] = field(default_factory=dict)
    user: dict[str, Any] = field(
        default_factory=lambda: {"name": "appuser", "uid": 1000}
    )
    entrypoint: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    custom_repos: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContainerManifest:
        """Create a manifest from a parsed JSON dict.

        Raises:
            ValueError: If required fields are missing.

        """
        for field_name in _REQUIRED_FIELDS:
            if field_name not in data:
                msg = f"container-manifest.json missing required field: {field_name}"
                raise ValueError(msg)

        return cls(
            base_image=data["base_image"],
            binary_name=data["binary_name"],
            runtime_packages=data.get("runtime_packages", []),
            expose_ports=data.get("expose_ports", []),
            health_check=data.get("health_check", {}),
            user=data.get("user", {"name": "appuser", "uid": 1000}),
            entrypoint=data.get("entrypoint", []),
            cmd=data.get("cmd", []),
            env=data.get("env", {}),
            labels=data.get("labels", {}),
            custom_repos=data.get("custom_repos", []),
        )


def load_manifest(path: Path) -> ContainerManifest:
    """Load and parse a container-manifest.json file.

    Args:
        path: Path to the manifest JSON file.

    Returns:
        Parsed ContainerManifest.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If required fields are missing.

    """
    if not path.exists():
        msg = f"Container manifest not found: {path}"
        raise FileNotFoundError(msg)

    data = json.loads(path.read_text())
    return ContainerManifest.from_dict(data)
