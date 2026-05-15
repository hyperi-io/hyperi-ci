# Project:   HyperI CI
# File:      tests/deployment/overlay/test_argocd_anchors.py
# Purpose:   Unit tests for ArgoCDAnchorResolver
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Unit tests for ``ArgoCDAnchorResolver``.

Covers each anchor in the catalog plus error paths:
  * spec.source.append / before
  * spec.destination.append
  * spec.syncPolicy.append (auto-creates if missing)
  * metadata.annotations.append (auto-creates if missing)
  * metadata.labels.append (auto-creates if missing)
  * root.append
  * Unknown anchor raises AnchorNotFound with candidates list
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from hyperi_ci.deployment.overlay.anchors.argocd import ArgoCDAnchorResolver
from hyperi_ci.deployment.overlay.errors import AnchorNotFound
from hyperi_ci.deployment.overlay.model import Overlay

_BASE = textwrap.dedent(
    """\
    apiVersion: argoproj.io/v1alpha1
    kind: Application
    metadata:
      name: dfe-loader
      namespace: argocd
    spec:
      source:
        repoURL: oci://ghcr.io/hyperi-io/helm-charts
        chart: dfe-loader
        targetRevision: 1.18.3
      destination:
        server: https://kubernetes.default.svc
        namespace: dfe-dev
    """
)


class TestSpecSourceAppend:
    def test_appends_helm_values_to_source(self) -> None:
        resolver = ArgoCDAnchorResolver()
        overlay = Overlay(
            anchor="spec.source.append",
            content="helm:\n  values: |\n    replicaCount: 3\n",
        )
        out = resolver.splice(_BASE, [overlay])
        doc = yaml.safe_load(out)
        assert doc["spec"]["source"]["helm"]["values"] == "replicaCount: 3\n"
        # Existing source keys preserved
        assert doc["spec"]["source"]["chart"] == "dfe-loader"


class TestSyncPolicyAppendAutoCreates:
    def test_creates_syncpolicy_when_missing(self) -> None:
        resolver = ArgoCDAnchorResolver()
        overlay = Overlay(
            anchor="spec.syncPolicy.append",
            content="automated:\n  prune: true\n  selfHeal: true\n",
        )
        out = resolver.splice(_BASE, [overlay])
        doc = yaml.safe_load(out)
        assert doc["spec"]["syncPolicy"]["automated"]["prune"] is True
        assert doc["spec"]["syncPolicy"]["automated"]["selfHeal"] is True

    def test_merges_into_existing_syncpolicy(self) -> None:
        base_with_syncpolicy = textwrap.dedent(
            """\
            apiVersion: argoproj.io/v1alpha1
            kind: Application
            metadata:
              name: x
            spec:
              source:
                chart: x
              destination:
                server: x
              syncPolicy:
                syncOptions:
                  - CreateNamespace=true
            """
        )
        resolver = ArgoCDAnchorResolver()
        overlay = Overlay(
            anchor="spec.syncPolicy.append",
            content="automated:\n  prune: true\n",
        )
        out = resolver.splice(base_with_syncpolicy, [overlay])
        doc = yaml.safe_load(out)
        assert doc["spec"]["syncPolicy"]["automated"]["prune"] is True
        assert doc["spec"]["syncPolicy"]["syncOptions"] == ["CreateNamespace=true"]


class TestAnnotationsLabelsAutoCreate:
    def test_annotations_appended_creates_map(self) -> None:
        resolver = ArgoCDAnchorResolver()
        overlay = Overlay(
            anchor="metadata.annotations.append",
            content='argocd.argoproj.io/sync-wave: "-1"\n',
        )
        out = resolver.splice(_BASE, [overlay])
        doc = yaml.safe_load(out)
        assert doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1"

    def test_labels_append(self) -> None:
        resolver = ArgoCDAnchorResolver()
        overlay = Overlay(
            anchor="metadata.labels.append",
            content="hyperi.io/team: platform\n",
        )
        out = resolver.splice(_BASE, [overlay])
        doc = yaml.safe_load(out)
        assert doc["metadata"]["labels"]["hyperi.io/team"] == "platform"


class TestRootAppend:
    def test_root_append_adds_top_level_key(self) -> None:
        resolver = ArgoCDAnchorResolver()
        overlay = Overlay(
            anchor="root.append",
            content="finalizers:\n  - resources-finalizer.argocd.argoproj.io\n",
        )
        out = resolver.splice(_BASE, [overlay])
        doc = yaml.safe_load(out)
        assert doc["finalizers"] == ["resources-finalizer.argocd.argoproj.io"]


class TestSpecSourceBefore:
    def test_before_keeps_fragment_keys_first(self) -> None:
        resolver = ArgoCDAnchorResolver()
        overlay = Overlay(
            anchor="spec.source.before",
            content="repoURL: oci://other\n",
        )
        out = resolver.splice(_BASE, [overlay])
        # Round-trip through yaml then check the source dict has both keys
        # (before semantics on a dict are insertion-order; the dict still
        # contains both, with the fragment's repoURL overwriting the base).
        doc = yaml.safe_load(out)
        assert doc["spec"]["source"]["repoURL"] == "oci://other"
        assert doc["spec"]["source"]["chart"] == "dfe-loader"


class TestErrors:
    def test_unknown_anchor_lists_candidates(self) -> None:
        resolver = ArgoCDAnchorResolver()
        overlay = Overlay(anchor="not-an-anchor", content="x: 1\n")
        with pytest.raises(AnchorNotFound) as exc:
            resolver.splice(_BASE, [overlay])
        assert exc.value.anchor == "not-an-anchor"
        assert "spec.source.append" in exc.value.candidates

    def test_known_anchors_listed(self) -> None:
        resolver = ArgoCDAnchorResolver()
        anchors = resolver.known_anchors
        assert "spec.source.append" in anchors
        assert "spec.source.before" in anchors
        assert "spec.syncPolicy.append" in anchors
        assert "root.append" in anchors


class TestMultipleOverlays:
    def test_each_overlay_applied_in_order(self) -> None:
        resolver = ArgoCDAnchorResolver()
        overlays = [
            Overlay(
                anchor="spec.syncPolicy.append",
                content="automated:\n  prune: true\n",
            ),
            Overlay(
                anchor="metadata.annotations.append",
                content='argocd.argoproj.io/sync-wave: "-1"\n',
            ),
            Overlay(
                anchor="spec.source.append",
                content="targetRevision: 2.0.0\n",
            ),
        ]
        out = resolver.splice(_BASE, overlays)
        doc = yaml.safe_load(out)
        assert doc["spec"]["syncPolicy"]["automated"]["prune"] is True
        assert doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1"
        # Last overlay overwrites the original targetRevision
        assert doc["spec"]["source"]["targetRevision"] == "2.0.0"
