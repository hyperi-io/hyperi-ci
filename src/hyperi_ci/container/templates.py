# Project:   HyperI CI
# File:      src/hyperi_ci/container/templates.py
# Purpose:   Dockerfile template rendering for Python and Node container builds
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Dockerfile templates for Python and Node container image builds."""

from __future__ import annotations

PYTHON_DOCKERFILE_TEMPLATE = """\
FROM python:{python_version}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Two-phase sync -- uv's recommended Docker pattern
# (https://docs.astral.sh/uv/guides/integration/docker/). The old
# single-phase `uv sync` copied only pyproject/uv.lock/README/src and then
# built the project, which fails for any package whose metadata references
# files not in that set -- e.g. `project.license-files = ["LICENSE",
# "NOTICE"]` -> "glob `LICENSE` did not match any files" (issue #51 RC2).
# Phase 1 installs ONLY the dependency graph (`--no-install-project`), so it
# needs neither the package source nor its license-files. Phase 2 copies the
# whole context and installs the project itself, by which point every
# metadata-referenced file is present. Phase 1 is also a cacheable layer keyed
# on the lockfile alone.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

FROM python:{python_version}-slim
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 1000 appuser
USER appuser
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \\
    CMD curl -sf http://localhost:{port}{health_path} > /dev/null || exit 1
ENTRYPOINT ["{entrypoint}"]
CMD ["{cmd}"]
"""

NODE_DOCKERFILE_TEMPLATE = """\
FROM node:{node_version}-slim AS builder
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --prod
COPY . .
RUN pnpm build

FROM gcr.io/distroless/nodejs{node_major}-debian12
WORKDIR /app
COPY --from=builder /app/dist /app/dist
COPY --from=builder /app/node_modules /app/node_modules
EXPOSE {port}
CMD ["dist/server.js"]
"""


def render_python_template(
    *,
    python_version: str = "3.12",
    port: int = 8000,
    health_path: str = "/healthz",
    entrypoint: str = "app",
    cmd: str = "run",
) -> str:
    """Render the Python Dockerfile template with the given parameters."""
    return PYTHON_DOCKERFILE_TEMPLATE.format(
        python_version=python_version,
        port=port,
        health_path=health_path,
        entrypoint=entrypoint,
        cmd=cmd,
    )


def render_node_template(
    *,
    node_version: str = "22",
    port: int = 3000,
) -> str:
    """Render the Node Dockerfile template with the given parameters."""
    node_major = node_version.split(".")[0]
    return NODE_DOCKERFILE_TEMPLATE.format(
        node_version=node_version,
        node_major=node_major,
        port=port,
    )
