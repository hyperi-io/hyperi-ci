# Project:   HyperI CI
# File:      src/hyperi_ci/container/templates.py
# Purpose:   Dockerfile template rendering for Python and Node container builds
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Dockerfile templates for Python and Node container image builds."""

from __future__ import annotations

PYTHON_DOCKERFILE_TEMPLATE = """\
FROM python:{python_version}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv venv .venv && uv sync --no-dev --frozen

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
