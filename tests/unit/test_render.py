# Project:   HyperI CI
# File:      tests/unit/test_render.py
# Purpose:   Tests for Helm chart rendering (kubeconform input prep)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.render.render_charts (helm mocked)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.quality import render


def _mock_helm(monkeypatch: pytest.MonkeyPatch, *, template_ok: bool = True) -> None:
    def _run(cmd, **kw):  # noqa: ANN001, ANN003
        if "template" in cmd:
            return SimpleNamespace(
                returncode=0 if template_ok else 1,
                stdout="apiVersion: apps/v1\nkind: Deployment\n" if template_ok else "",
            )
        return SimpleNamespace(returncode=0, stdout="")  # dependency build

    monkeypatch.setattr(render, "run_cmd", _run)


class TestRenderCharts:
    def test_renders_each_chart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_helm(monkeypatch)
        chart = tmp_path / "svc"
        chart.mkdir()
        out = tmp_path / "out"
        rendered = render.render_charts([chart], out)
        assert len(rendered) == 1
        assert rendered[0].name == "svc.rendered.yaml"
        assert "kind: Deployment" in rendered[0].read_text(encoding="utf-8")

    def test_skips_unrenderable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_helm(monkeypatch, template_ok=False)
        chart = tmp_path / "svc"
        chart.mkdir()
        rendered = render.render_charts([chart], tmp_path / "out")
        assert rendered == []


class TestReleaseName:
    @pytest.mark.parametrize(
        ("dirname", "expected"),
        [
            ("web", "web"),
            ("Chart", "chart"),  # capital dir would break `helm template`
            ("my_chart", "my-chart"),  # underscore is invalid in a release name
            ("UPPER_Case", "upper-case"),
            ("--x--", "x"),
            ("___", "chart"),  # empty after sanitising -> fallback
        ],
    )
    def test_sanitises(self, tmp_path: Path, dirname: str, expected: str) -> None:
        assert render._release_name(tmp_path / dirname) == expected

    def test_render_uses_sanitised_release_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def _run(cmd, **kw):  # noqa: ANN001, ANN003
            if "template" in cmd:
                captured["cmd"] = cmd
                return SimpleNamespace(returncode=0, stdout="kind: X\n")
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(render, "run_cmd", _run)
        chart = tmp_path / "Chart"  # capital - would fail helm without sanitising
        chart.mkdir()
        render.render_charts([chart], tmp_path / "out")
        # `helm template <release> <chart>` - release name must be lowercase.
        assert captured["cmd"][2] == "chart"
