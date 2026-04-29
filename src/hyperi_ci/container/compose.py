# Project:   HyperI CI
# File:      src/hyperi_ci/container/compose.py
# Purpose:   Compose Dockerfile from rustlib deployment contract manifest
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Compose multi-stage Dockerfile from contract manifest + cargo-chef build stages."""

from __future__ import annotations

import json

from hyperi_ci.container.manifest import ContainerManifest


def compose_contract_dockerfile(
    manifest: ContainerManifest,
    rust_version: str = "stable",
) -> str:
    """Compose a complete multi-stage Dockerfile from a contract manifest.

    Generates:
      - chef stage: install cargo-chef
      - planner stage: prepare recipe
      - builder stage: cook recipe, build binary
      - runtime stage: from manifest base image, install deps, copy binary

    Args:
        manifest: Parsed container manifest from rustlib.
        rust_version: Rust toolchain version for build stages.

    Returns:
        Complete Dockerfile content as string.

    """
    sections = [
        _chef_stage(rust_version),
        _planner_stage(),
        _builder_stage(manifest.binary_name),
        _runtime_stage(manifest),
    ]
    return "\n".join(sections)


def _chef_stage(rust_version: str) -> str:
    return f"""\
FROM rust:{rust_version}-slim AS chef
RUN cargo install cargo-chef
WORKDIR /app"""


def _planner_stage() -> str:
    return """\

FROM chef AS planner
COPY . .
RUN cargo chef prepare --recipe-path recipe.json"""


def _builder_stage(binary_name: str) -> str:
    return f"""\

FROM chef AS builder
COPY --from=planner /app/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json
COPY . .
RUN cargo build --release --bin {binary_name}"""


def _runtime_stage(manifest: ContainerManifest) -> str:
    lines = [f"\nFROM {manifest.base_image} AS runtime"]

    if manifest.runtime_packages:
        pkg_list = " ".join(manifest.runtime_packages)
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"    {pkg_list} && rm -rf /var/lib/apt/lists/*"
        )

    lines.append("WORKDIR /app")
    lines.append(
        f"COPY --from=builder /app/target/release/{manifest.binary_name} "
        f"/usr/local/bin/{manifest.binary_name}"
    )

    for key, value in manifest.env.items():
        lines.append(f'ENV {key}="{value}"')

    user = manifest.user
    uid = user.get("uid", 1000)
    name = user.get("name", "appuser")
    lines.append(f"RUN useradd --create-home --uid {uid} {name}")
    lines.append(f"USER {name}")

    for port in manifest.expose_ports:
        lines.append(f"EXPOSE {port}")

    hc = manifest.health_check
    if hc:
        path = hc.get("path", "/healthz")
        hc_port = hc.get("port", 8080)
        interval = hc.get("interval", 30)
        timeout = hc.get("timeout", 3)
        start_period = hc.get("start_period", 10)
        retries = hc.get("retries", 3)
        lines.append(
            f"HEALTHCHECK --interval={interval}s --timeout={timeout}s "
            f"--start-period={start_period}s --retries={retries} \\\n"
            f"    CMD curl -sf http://localhost:{hc_port}{path} > /dev/null || exit 1"
        )

    if manifest.entrypoint:
        ep = json.dumps(manifest.entrypoint)
        lines.append(f"ENTRYPOINT {ep}")

    if manifest.cmd:
        cmd = json.dumps(manifest.cmd)
        lines.append(f"CMD {cmd}")

    return "\n".join(lines)
