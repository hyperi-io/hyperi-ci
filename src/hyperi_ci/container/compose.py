# Project:   HyperI CI
# File:      src/hyperi_ci/container/compose.py
# Purpose:   Compose Dockerfile from scalo deployment contract manifest
#
# License:   BUSL-1.1
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
        manifest: Parsed container manifest from scalo.
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


# Mirrors `tools.cargo-chef` in config/versions.yaml - the SSoT.
# hyperi-ci:pin tools.cargo-chef
_CARGO_CHEF_VERSION = "v0.1.77"


# rustup channel names. Docker Hub's `rust` image publishes NO tag for any of
# them (`rust:stable-slim`, `rust:nightly-slim`, `rust:beta-slim` all 404) -
# only `slim`, `1-slim`, `1.97-slim`. `_detect_rust_version()` returns the
# `channel` from rust-toolchain.toml verbatim, so every one of these is
# reachable, and the ARC image installs nightly, so it is a live configuration.
_RUST_CHANNELS = ("stable", "beta", "nightly")


def _rust_slim_tag(rust_version: str) -> str:
    """Map a toolchain channel or version to a REAL `rust` image tag.

    `slim` means current stable, so a `stable` channel maps cleanly. `beta` /
    `nightly` (and dated nightlies like `nightly-2026-01-01`) have NO image at
    all: they build on the stable image and are switched with `rustup`, which
    _chef_stage emits. Returning `<channel>-slim` for them would reproduce the
    exact unresolvable-image failure this function exists to prevent - the bug
    was never about the word "stable", it was about channels not being tags.
    """
    if not rust_version or rust_version in ("latest", *_RUST_CHANNELS):
        return "slim"
    if rust_version.startswith(_RUST_CHANNELS):  # e.g. nightly-2026-01-01
        return "slim"
    return f"{rust_version}-slim"


def _rust_channel_switch(rust_version: str) -> str:
    """`rustup default` line when the toolchain is a non-stable channel, else "".

    The image is always the stable one (see _rust_slim_tag), so a repo pinning
    `nightly` needs rustup pointed at it explicitly or the build silently uses
    stable - which would be a WRONG build, not a failed one.
    """
    if rust_version.startswith(("beta", "nightly")):
        return f"RUN rustup toolchain install {rust_version} && rustup default {rust_version}\n"
    return ""


def _chef_stage(rust_version: str) -> str:
    # cargo-chef ships a prebuilt musl-static binary, so fetch it instead of
    # compiling it into every container build that misses the layer cache.
    # Measured on this stage alone, `docker build --no-cache`: 24.6s compiling
    # vs 3.6s fetching (dev laptop; a 4GB CI runner is slower at the compile
    # and no faster at the download).
    #
    # rust:slim carries tar but NOT curl or xz, and Docker's ADD does not
    # auto-extract a REMOTE archive - hence the explicit apt step.
    # uname -m (not TARGETARCH) because ADD/ARG cannot do the arch mapping and
    # this RUN executes on the target platform under buildx.
    return f"""\
FROM rust:{_rust_slim_tag(rust_version)} AS chef
ARG CARGO_CHEF_VERSION={_CARGO_CHEF_VERSION}
RUN set -eux; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends curl xz-utils ca-certificates; \\
    rm -rf /var/lib/apt/lists/*; \\
    case "$(uname -m)" in \\
      x86_64)  chef_target=x86_64-unknown-linux-musl ;; \\
      aarch64) chef_target=aarch64-unknown-linux-musl ;; \\
      *) echo "unsupported arch $(uname -m) for cargo-chef" >&2; exit 1 ;; \\
    esac; \\
    curl -sSfL \\
      "https://github.com/LukeMathWalker/cargo-chef/releases/download/${{CARGO_CHEF_VERSION}}/cargo-chef-${{chef_target}}.tar.xz" \\
      | tar -xJ -C "$CARGO_HOME/bin" --strip-components=1 \\
        "cargo-chef-${{chef_target}}/cargo-chef"; \\
    cargo chef --version
{_rust_channel_switch(rust_version)}WORKDIR /app"""


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
    # Ubuntu 24.04 ships with `ubuntu` user at UID 1000. If we're claiming UID
    # 1000, remove the existing user first; otherwise `useradd --uid 1000`
    # fails with "UID is not unique" (exit 4). Harmless on images without a
    # ubuntu user (`userdel` returns 6, `|| true` swallows it).
    if uid == 1000:
        lines.append(
            f"RUN (userdel -r ubuntu 2>/dev/null || true) "
            f"&& useradd --create-home --uid {uid} {name}"
        )
    else:
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
