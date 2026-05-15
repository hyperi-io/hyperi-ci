# Project:   HyperI CI
# File:      tests/deployment/overlay/test_cli.py
# Purpose:   Integration tests for `hyperi-ci overlay-render` CLI
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Integration tests for the overlay-render command.

Mocks subprocess (the consumer binary's emit-* subcommand) but uses
real fixture content + real overlay processing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci.deployment.overlay.cli import render


_BASE_DOCKERFILE = textwrap.dedent(
    """\
    FROM ubuntu:24.04
    RUN apt-get update && apt-get install -y curl
    COPY myapp /usr/local/bin/myapp
    USER appuser
    ENTRYPOINT ["myapp"]
    """
)

_BASE_ARGOCD = textwrap.dedent(
    """\
    apiVersion: argoproj.io/v1alpha1
    kind: Application
    metadata:
      name: myapp
    spec:
      source:
        repoURL: oci://ghcr.io/hyperi-io/helm-charts
        chart: myapp
        targetRevision: 1.0.0
      destination:
        server: https://kubernetes.default.svc
        namespace: default
    """
)


def _make_project(tmp_path: Path, hyperi_ci_yaml: str) -> Path:
    """Create a minimal project dir with .hyperi-ci.yaml."""
    project = tmp_path / "myapp"
    project.mkdir()
    (project / ".hyperi-ci.yaml").write_text(
        hyperi_ci_yaml, encoding="utf-8"
    )
    return project


def _mock_emit_dockerfile(*args, **kwargs):
    """subprocess.run mock that returns the base Dockerfile."""

    class _Res:
        returncode = 0
        stdout = _BASE_DOCKERFILE
        stderr = ""

    return _Res()


def _mock_emit_argocd(*args, **kwargs):
    class _Res:
        returncode = 0
        stdout = _BASE_ARGOCD
        stderr = ""

    return _Res()


class TestRenderDockerfile:
    def test_no_overlays_returns_base(self, tmp_path: Path) -> None:
        project = _make_project(
            tmp_path,
            "publish:\n  container:\n    enabled: true\n",
        )
        out_path = tmp_path / "out.dockerfile"
        with patch(
            "hyperi_ci.deployment.overlay.cli.subprocess.run",
            side_effect=_mock_emit_dockerfile,
        ):
            rc = render(
                kind="dockerfile",
                project_dir=project,
                output=out_path,
            )
        assert rc == 0
        # No overlays declared → output is the base Dockerfile.
        assert out_path.read_text(encoding="utf-8") == _BASE_DOCKERFILE

    def test_with_overlay_splices_at_anchor(self, tmp_path: Path) -> None:
        project = _make_project(
            tmp_path,
            textwrap.dedent(
                """\
                publish:
                  container:
                    enabled: true
                    overlays:
                      - anchor: before-user
                        content: |
                          # vector-download
                          RUN echo hi
                """
            ),
        )
        out_path = tmp_path / "out.dockerfile"
        with patch(
            "hyperi_ci.deployment.overlay.cli.subprocess.run",
            side_effect=_mock_emit_dockerfile,
        ):
            rc = render(
                kind="dockerfile",
                project_dir=project,
                output=out_path,
            )
        assert rc == 0
        text = out_path.read_text(encoding="utf-8")
        assert "# vector-download" in text
        # Overlay precedes USER.
        assert text.index("# vector-download") < text.index("USER appuser")

    def test_subprocess_failure_propagates(self, tmp_path: Path) -> None:
        project = _make_project(
            tmp_path,
            "publish:\n  container:\n    enabled: true\n",
        )

        def _fail(*args, **kwargs):
            class _Res:
                returncode = 1
                stdout = ""
                stderr = "boom"

            return _Res()

        with patch(
            "hyperi_ci.deployment.overlay.cli.subprocess.run",
            side_effect=_fail,
        ):
            rc = render(
                kind="dockerfile",
                project_dir=project,
                output=tmp_path / "out.dockerfile",
            )
        assert rc == 1


class TestRenderArgocd:
    def test_no_overlays_emits_base(self, tmp_path: Path) -> None:
        project = _make_project(
            tmp_path,
            "publish:\n  argocd:\n    enabled: true\n",
        )
        out_path = tmp_path / "argocd.yaml"
        with patch(
            "hyperi_ci.deployment.overlay.cli.subprocess.run",
            side_effect=_mock_emit_argocd,
        ):
            rc = render(
                kind="argocd",
                project_dir=project,
                output=out_path,
            )
        assert rc == 0
        # YAML round-trips identically (subject to formatting).
        import yaml

        produced = yaml.safe_load(out_path.read_text(encoding="utf-8"))
        original = yaml.safe_load(_BASE_ARGOCD)
        assert produced == original

    def test_overlay_appends_helm_values(self, tmp_path: Path) -> None:
        project = _make_project(
            tmp_path,
            textwrap.dedent(
                """\
                publish:
                  argocd:
                    enabled: true
                    overlays:
                      - anchor: spec.source.append
                        content: |
                          helm:
                            values: |
                              replicaCount: 5
                """
            ),
        )
        out_path = tmp_path / "argocd.yaml"
        with patch(
            "hyperi_ci.deployment.overlay.cli.subprocess.run",
            side_effect=_mock_emit_argocd,
        ):
            rc = render(
                kind="argocd",
                project_dir=project,
                output=out_path,
            )
        assert rc == 0
        import yaml

        doc = yaml.safe_load(out_path.read_text(encoding="utf-8"))
        assert doc["spec"]["source"]["helm"]["values"] == "replicaCount: 5\n"


class TestRenderUnknownKind:
    def test_returns_2_for_unknown_kind(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, "")
        rc = render(
            kind="bogus",
            project_dir=project,
            output=None,
        )
        assert rc == 2
