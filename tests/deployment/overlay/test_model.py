# Project:   HyperI CI
# File:      tests/deployment/overlay/test_model.py
# Purpose:   Unit tests for overlay declaration models + parsers
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Unit tests for ``hyperi_ci.deployment.overlay.model``.

Covers:
  * Simple overlay parsing (Dockerfile / ArgoCD)
  * Helm overlay parsing (adds + patches)
  * Validation errors with structured location info
  * Resolution of inline ``content`` and ``file:`` references
  * Missing-file errors
  * Top-level ``parse_overlay_config`` over a full publish: block
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.deployment.overlay.errors import (
    OverlayFileMissing,
    OverlayValidationError,
)
from hyperi_ci.deployment.overlay.model import (
    HelmOverlays,
    Overlay,
    OverlayConfig,
    overlay_grouped_by_anchor,
    parse_helm_overlays,
    parse_overlay_config,
    parse_simple_overlays,
)


class TestSimpleOverlayParsing:
    def test_parses_inline_content(self) -> None:
        raw = [{"anchor": "before-user", "content": "RUN echo hi\n"}]
        out = parse_simple_overlays(raw, artefact="container")
        assert len(out) == 1
        assert out[0].anchor == "before-user"
        assert out[0].content == "RUN echo hi\n"
        assert out[0].file is None

    def test_parses_file_reference(self) -> None:
        raw = [{"anchor": "before-user", "file": "overlays/x.dockerfile"}]
        out = parse_simple_overlays(raw, artefact="container")
        assert out[0].file == Path("overlays/x.dockerfile")
        assert out[0].content == ""

    def test_rejects_both_content_and_file(self) -> None:
        raw = [
            {
                "anchor": "before-user",
                "content": "RUN x",
                "file": "ovl.dockerfile",
            }
        ]
        with pytest.raises(OverlayValidationError) as exc:
            parse_simple_overlays(raw, artefact="container")
        assert "exactly one of" in str(exc.value)
        assert "publish.container.overlays[0]" in str(exc.value)

    def test_rejects_neither_content_nor_file(self) -> None:
        raw = [{"anchor": "before-user"}]
        with pytest.raises(OverlayValidationError) as exc:
            parse_simple_overlays(raw, artefact="container")
        assert "exactly one of" in str(exc.value)

    def test_rejects_missing_anchor(self) -> None:
        raw = [{"content": "RUN echo hi\n"}]
        with pytest.raises(OverlayValidationError) as exc:
            parse_simple_overlays(raw, artefact="argocd")
        assert "missing required string `anchor`" in str(exc.value)
        assert "publish.argocd.overlays[0]" in str(exc.value)

    def test_rejects_non_list(self) -> None:
        with pytest.raises(OverlayValidationError) as exc:
            parse_simple_overlays({"oops": "not a list"}, artefact="container")
        assert "overlays must be a list" in str(exc.value)

    def test_none_returns_empty_tuple(self) -> None:
        assert parse_simple_overlays(None, artefact="container") == ()

    def test_multiple_overlays_indexed_correctly(self) -> None:
        raw = [
            {"anchor": "before-user", "content": "RUN one\n"},
            {"anchor": "before-user", "content": "RUN two\n"},
            {"anchor": "missing-fields"},  # invalid - both content+file empty
        ]
        with pytest.raises(OverlayValidationError) as exc:
            parse_simple_overlays(raw, artefact="container")
        # Index in the error message should be 2, not 0 or 1.
        assert "publish.container.overlays[2]" in str(exc.value)


class TestOverlayResolve:
    def test_inline_content_resolves_directly(self, tmp_path: Path) -> None:
        o = Overlay(anchor="before-user", content="RUN inline\n")
        assert (
            o.resolve(base_dir=tmp_path, artefact="container", index=0)
            == "RUN inline\n"
        )

    def test_file_resolves_relative_to_base_dir(self, tmp_path: Path) -> None:
        frag = tmp_path / "ovl.dockerfile"
        frag.write_text("RUN from-file\n", encoding="utf-8")
        o = Overlay(anchor="before-user", file=Path("ovl.dockerfile"))
        out = o.resolve(base_dir=tmp_path, artefact="container", index=0)
        assert out == "RUN from-file\n"

    def test_file_absolute_path_works(self, tmp_path: Path) -> None:
        frag = tmp_path / "abs.dockerfile"
        frag.write_text("RUN abs\n", encoding="utf-8")
        o = Overlay(anchor="before-user", file=frag)
        out = o.resolve(base_dir=Path("/elsewhere"), artefact="container", index=0)
        assert out == "RUN abs\n"

    def test_missing_file_raises_with_path(self, tmp_path: Path) -> None:
        o = Overlay(anchor="before-user", file=Path("does-not-exist.df"))
        with pytest.raises(OverlayFileMissing) as exc:
            o.resolve(base_dir=tmp_path, artefact="container", index=3)
        assert exc.value.path == tmp_path / "does-not-exist.df"
        assert exc.value.overlay_index == 3
        assert "publish.container.overlays[3]" in str(exc.value)

    def test_unresolvable_neither_raises(self, tmp_path: Path) -> None:
        o = Overlay(anchor="before-user")
        with pytest.raises(OverlayValidationError):
            o.resolve(base_dir=tmp_path, artefact="container", index=0)


class TestHelmParsing:
    def test_parses_adds_and_patches(self) -> None:
        raw = {
            "adds": [
                {"path": "templates/x.yaml", "content": "kind: ConfigMap\n"},
                {"path": "templates/y.yaml", "file": "helm.d/y.yaml"},
            ],
            "patches": [
                {
                    "target": {"kind": "Deployment", "name": "myapp"},
                    "patch": "spec:\n  replicas: 3\n",
                },
                {
                    "target": {"kind": "Service", "name": "myapp"},
                    "patch_file": "helm.d/svc.yaml",
                },
            ],
        }
        out = parse_helm_overlays(raw)
        assert isinstance(out, HelmOverlays)
        assert len(out.adds) == 2
        assert len(out.patches) == 2
        assert out.adds[0].path == "templates/x.yaml"
        assert out.adds[0].content == "kind: ConfigMap\n"
        assert out.adds[1].file == Path("helm.d/y.yaml")
        assert out.patches[0].target == {"kind": "Deployment", "name": "myapp"}
        assert out.patches[1].patch_file == Path("helm.d/svc.yaml")

    def test_none_returns_empty(self) -> None:
        out = parse_helm_overlays(None)
        assert out == HelmOverlays()

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(OverlayValidationError):
            parse_helm_overlays(["not", "a", "dict"])

    def test_helm_add_requires_path(self) -> None:
        with pytest.raises(OverlayValidationError) as exc:
            parse_helm_overlays(
                {"adds": [{"content": "kind: x"}]}
            )
        assert "missing required string `path`" in str(exc.value)

    def test_helm_add_rejects_both_content_and_file(self) -> None:
        with pytest.raises(OverlayValidationError):
            parse_helm_overlays(
                {
                    "adds": [
                        {
                            "path": "templates/x.yaml",
                            "content": "x",
                            "file": "y",
                        }
                    ]
                }
            )

    def test_helm_patch_requires_target(self) -> None:
        with pytest.raises(OverlayValidationError) as exc:
            parse_helm_overlays(
                {"patches": [{"patch": "spec:\n  x: 1\n"}]}
            )
        assert "missing required mapping `target`" in str(exc.value)


class TestParseOverlayConfig:
    def test_parses_full_publish_block(self) -> None:
        publish = {
            "container": {
                "overlays": [
                    {"anchor": "before-user", "content": "RUN x\n"}
                ]
            },
            "helm": {
                "overlays": {
                    "adds": [
                        {"path": "templates/x.yaml", "content": "kind: x\n"}
                    ]
                }
            },
            "argocd": {
                "overlays": [
                    {
                        "anchor": "spec.source.append",
                        "content": "chart: x\n",
                    }
                ]
            },
        }
        cfg = parse_overlay_config(publish)
        assert isinstance(cfg, OverlayConfig)
        assert len(cfg.container) == 1
        assert len(cfg.helm.adds) == 1
        assert len(cfg.argocd) == 1

    def test_empty_publish_returns_empty_config(self) -> None:
        assert parse_overlay_config({}) == OverlayConfig()
        assert parse_overlay_config(None) == OverlayConfig()

    def test_no_overlays_section_returns_empty_tuples(self) -> None:
        # publish has container/helm/argocd but no overlays declared
        cfg = parse_overlay_config(
            {
                "container": {"enabled": True},
                "helm": {"enabled": True},
                "argocd": {"enabled": True},
            }
        )
        assert cfg.container == ()
        assert cfg.helm == HelmOverlays()
        assert cfg.argocd == ()


class TestGroupByAnchor:
    def test_preserves_declaration_order(self) -> None:
        overlays = [
            Overlay(anchor="before-user", content="A"),
            Overlay(anchor="after-base-deps", content="B"),
            Overlay(anchor="before-user", content="C"),
        ]
        grouped = overlay_grouped_by_anchor(overlays)
        assert list(grouped.keys()) == ["before-user", "after-base-deps"]
        assert [o.content for o in grouped["before-user"]] == ["A", "C"]
        assert [o.content for o in grouped["after-base-deps"]] == ["B"]
