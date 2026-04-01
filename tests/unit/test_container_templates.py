# Project:   HyperI CI
# File:      tests/unit/test_container_templates.py
# Purpose:   Tests for Dockerfile template rendering
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for container Dockerfile template rendering."""

from __future__ import annotations

from hyperi_ci.container.templates import render_node_template, render_python_template


def test_python_template_defaults() -> None:
    result = render_python_template()
    assert "FROM python:3.12-slim" in result
    assert "COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv" in result
    assert "EXPOSE 8000" in result
    assert 'ENTRYPOINT ["app"]' in result
    assert 'CMD ["run"]' in result
    assert "HEALTHCHECK" in result and "/healthz" in result
    assert "useradd" in result
    assert "USER appuser" in result


def test_python_template_custom_port() -> None:
    result = render_python_template(
        python_version="3.11",
        port=9000,
        health_path="/health",
        entrypoint="myapp",
        cmd="serve",
    )
    assert "FROM python:3.11-slim" in result
    assert "EXPOSE 9000" in result
    assert "/health" in result
    assert 'ENTRYPOINT ["myapp"]' in result
    assert 'CMD ["serve"]' in result


def test_node_template_defaults() -> None:
    result = render_node_template()
    assert "FROM node:22-slim" in result
    assert "corepack enable" in result
    assert "pnpm install" in result
    assert "gcr.io/distroless/nodejs22-debian12" in result
    assert "EXPOSE 3000" in result


def test_node_template_custom() -> None:
    result = render_node_template(node_version="20.11.0", port=4000)
    assert "EXPOSE 4000" in result
    assert "gcr.io/distroless/nodejs20-debian12" in result
    assert "nodejs20.11.0" not in result
