# Project:   HyperI CI
# File:      tests/deployment/overlay/test_helm_anchors.py
# Purpose:   Unit tests for HelmAnchorResolver
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Unit tests for ``HelmAnchorResolver``.

Covers:
  * ``apply_adds`` writes new templates into chart_dir/templates/
  * ``apply_adds`` rejects collision with existing chart files
  * ``apply_patches`` strategic-merges a single matched document
  * Patch target selectors: kind+name, kind+name+namespace, labels
  * AnchorNotFound when target doesn't match any document
  * OverlayValidationError when target matches multiple documents
  * Strategic-merge semantics (deep merge, list replace, $patch:delete)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from hyperi_ci.deployment.overlay.anchors.helm import HelmAnchorResolver
from hyperi_ci.deployment.overlay.errors import (
    AnchorNotFound,
    OverlayValidationError,
)
from hyperi_ci.deployment.overlay.model import (
    HelmAddOverlay,
    HelmPatchOverlay,
)

# A representative two-resource rendered chart (Deployment + Service).
_RENDERED_CHART = textwrap.dedent(
    """\
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: dfe-transform-vector
      namespace: dfe-dev
    spec:
      replicas: 1
      template:
        spec:
          containers:
            - name: app
              image: ghcr.io/hyperi-io/dfe-transform-vector:1.0.13
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: dfe-transform-vector
      namespace: dfe-dev
    spec:
      ports:
        - port: 8080
          targetPort: 8080
    """
)


class TestApplyAdds:
    def test_writes_template_into_chart_dir(self, tmp_path: Path) -> None:
        chart = tmp_path / "chart"
        (chart / "templates").mkdir(parents=True)
        # Existing file - should NOT be touched.
        (chart / "templates" / "deployment.yaml").write_text(
            "kind: Deployment\n", encoding="utf-8"
        )

        add = HelmAddOverlay(
            path="templates/vector-pvc.yaml",
            content="kind: PersistentVolumeClaim\n",
        )
        resolver = HelmAnchorResolver()
        written = resolver.apply_adds(
            chart_dir=chart, adds=[add], base_dir=tmp_path
        )
        assert written == [chart / "templates" / "vector-pvc.yaml"]
        assert (chart / "templates" / "vector-pvc.yaml").read_text(
            encoding="utf-8"
        ) == "kind: PersistentVolumeClaim\n"
        # Existing file untouched.
        assert (chart / "templates" / "deployment.yaml").read_text(
            encoding="utf-8"
        ) == "kind: Deployment\n"

    def test_resolves_file_reference(self, tmp_path: Path) -> None:
        chart = tmp_path / "chart"
        (chart / "templates").mkdir(parents=True)
        frag = tmp_path / "helm.d"
        frag.mkdir()
        (frag / "pvc.yaml").write_text("kind: PVC\n", encoding="utf-8")

        add = HelmAddOverlay(
            path="templates/pvc.yaml", file=Path("helm.d/pvc.yaml")
        )
        resolver = HelmAnchorResolver()
        resolver.apply_adds(chart_dir=chart, adds=[add], base_dir=tmp_path)
        assert (chart / "templates" / "pvc.yaml").read_text(
            encoding="utf-8"
        ) == "kind: PVC\n"

    def test_rejects_collision_with_existing_file(self, tmp_path: Path) -> None:
        chart = tmp_path / "chart"
        (chart / "templates").mkdir(parents=True)
        (chart / "templates" / "deployment.yaml").write_text(
            "kind: Deployment\n", encoding="utf-8"
        )

        add = HelmAddOverlay(
            path="templates/deployment.yaml", content="kind: WrongDeployment\n"
        )
        resolver = HelmAnchorResolver()
        with pytest.raises(OverlayValidationError) as exc:
            resolver.apply_adds(
                chart_dir=chart, adds=[add], base_dir=tmp_path
            )
        assert "would overwrite existing chart file" in str(exc.value)
        # Original untouched.
        assert (
            chart / "templates" / "deployment.yaml"
        ).read_text(encoding="utf-8") == "kind: Deployment\n"

    def test_creates_intermediate_dirs(self, tmp_path: Path) -> None:
        chart = tmp_path / "chart"
        chart.mkdir()
        # Note: templates/ doesn't exist yet
        add = HelmAddOverlay(
            path="templates/extra/secrets.yaml", content="kind: Secret\n"
        )
        resolver = HelmAnchorResolver()
        resolver.apply_adds(chart_dir=chart, adds=[add], base_dir=tmp_path)
        assert (chart / "templates" / "extra" / "secrets.yaml").read_text(
            encoding="utf-8"
        ) == "kind: Secret\n"


class TestApplyPatches:
    def test_strategic_merge_adds_replicas(self, tmp_path: Path) -> None:
        patch = HelmPatchOverlay(
            target={"kind": "Deployment", "name": "dfe-transform-vector"},
            patch="spec:\n  replicas: 5\n",
        )
        resolver = HelmAnchorResolver()
        out = resolver.apply_patches(
            rendered_yaml=_RENDERED_CHART,
            patches=[patch],
            base_dir=tmp_path,
        )
        docs = list(yaml.safe_load_all(out))
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        assert deployment["spec"]["replicas"] == 5
        # Service still untouched
        service = next(d for d in docs if d["kind"] == "Service")
        assert service["spec"]["ports"] == [
            {"port": 8080, "targetPort": 8080}
        ]

    def test_target_with_namespace_disambiguates(self, tmp_path: Path) -> None:
        # Two deployments with same name in different namespaces
        rendered = textwrap.dedent(
            """\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: app
              namespace: dev
            spec:
              replicas: 1
            ---
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: app
              namespace: prod
            spec:
              replicas: 1
            """
        )
        patch = HelmPatchOverlay(
            target={"kind": "Deployment", "name": "app", "namespace": "prod"},
            patch="spec:\n  replicas: 10\n",
        )
        resolver = HelmAnchorResolver()
        out = resolver.apply_patches(
            rendered_yaml=rendered, patches=[patch], base_dir=tmp_path
        )
        docs = list(yaml.safe_load_all(out))
        prod = next(
            d for d in docs if d["metadata"]["namespace"] == "prod"
        )
        dev = next(d for d in docs if d["metadata"]["namespace"] == "dev")
        assert prod["spec"]["replicas"] == 10
        assert dev["spec"]["replicas"] == 1

    def test_target_no_match_raises(self, tmp_path: Path) -> None:
        patch = HelmPatchOverlay(
            target={"kind": "Deployment", "name": "not-here"},
            patch="spec:\n  replicas: 1\n",
        )
        resolver = HelmAnchorResolver()
        with pytest.raises(AnchorNotFound) as exc:
            resolver.apply_patches(
                rendered_yaml=_RENDERED_CHART, patches=[patch], base_dir=tmp_path
            )
        assert "Helm" == exc.value.artefact
        assert "Deployment" in exc.value.candidates
        assert "Service" in exc.value.candidates

    def test_target_multiple_matches_raises(self, tmp_path: Path) -> None:
        rendered = textwrap.dedent(
            """\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: app
              namespace: dev
            spec: {}
            ---
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: app
              namespace: prod
            spec: {}
            """
        )
        patch = HelmPatchOverlay(
            target={"kind": "Deployment", "name": "app"},  # ambiguous: 2 namespaces
            patch="spec:\n  replicas: 1\n",
        )
        resolver = HelmAnchorResolver()
        with pytest.raises(OverlayValidationError) as exc:
            resolver.apply_patches(
                rendered_yaml=rendered, patches=[patch], base_dir=tmp_path
            )
        assert "matched 2 rendered documents" in str(exc.value)

    def test_label_selector_in_target(self, tmp_path: Path) -> None:
        rendered = textwrap.dedent(
            """\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: app
              labels:
                tier: frontend
            spec:
              replicas: 1
            ---
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: app
              labels:
                tier: backend
            spec:
              replicas: 1
            """
        )
        patch = HelmPatchOverlay(
            target={
                "kind": "Deployment",
                "name": "app",
                "labels": {"tier": "backend"},
            },
            patch="spec:\n  replicas: 7\n",
        )
        resolver = HelmAnchorResolver()
        out = resolver.apply_patches(
            rendered_yaml=rendered, patches=[patch], base_dir=tmp_path
        )
        docs = list(yaml.safe_load_all(out))
        backend = next(
            d for d in docs if d["metadata"]["labels"]["tier"] == "backend"
        )
        frontend = next(
            d for d in docs if d["metadata"]["labels"]["tier"] == "frontend"
        )
        assert backend["spec"]["replicas"] == 7
        assert frontend["spec"]["replicas"] == 1

    def test_dollar_patch_delete_removes_key(self, tmp_path: Path) -> None:
        rendered = textwrap.dedent(
            """\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: app
              annotations:
                old: value
                keep: me
            spec:
              replicas: 1
            """
        )
        patch = HelmPatchOverlay(
            target={"kind": "Deployment", "name": "app"},
            patch=textwrap.dedent(
                """\
                metadata:
                  annotations:
                    old:
                      $patch: delete
                """
            ),
        )
        resolver = HelmAnchorResolver()
        out = resolver.apply_patches(
            rendered_yaml=rendered, patches=[patch], base_dir=tmp_path
        )
        doc = next(iter(yaml.safe_load_all(out)))
        assert "old" not in doc["metadata"]["annotations"]
        assert doc["metadata"]["annotations"]["keep"] == "me"

    def test_empty_patches_returns_input_unchanged(self, tmp_path: Path) -> None:
        resolver = HelmAnchorResolver()
        out = resolver.apply_patches(
            rendered_yaml=_RENDERED_CHART, patches=[], base_dir=tmp_path
        )
        # YAML serialises differently but parses to the same documents.
        original_docs = list(yaml.safe_load_all(_RENDERED_CHART))
        out_docs = list(yaml.safe_load_all(out))
        assert original_docs == out_docs
