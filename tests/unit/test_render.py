# Project:   HyperI CI
# File:      tests/unit/test_render.py
# Purpose:   Tests for Helm chart and kustomization rendering
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.render.

Real helm and kustomize run where installed; the timeout and dependency-build
paths stub run_cmd, because a chart cannot provoke them.
"""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyperi_ci.common import stage_tree
from hyperi_ci.config import CIConfig
from hyperi_ci.quality import findings, render
from hyperi_ci.quality.targets import kustomization_refs

HELM = shutil.which("helm") or "helm"
KUSTOMIZE = shutil.which("kustomize") or "kustomize"
needs_helm = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is not installed"
)
needs_kustomize = pytest.mark.skipif(
    shutil.which("kustomize") is None, reason="kustomize is not installed"
)

_CONFIGMAP = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}
data:
  value: {{ .Values.value | quote }}
"""


def _chart(root: Path, name: str = "svc", template: str = _CONFIGMAP) -> Path:
    chart = root / name
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n", encoding="utf-8"
    )
    (chart / "values.yaml").write_text("value: default\n", encoding="utf-8")
    (chart / "templates" / "cm.yaml").write_text(template, encoding="utf-8")
    return chart


def _render_chart(chart: Path, helm: str = HELM, **kw: Any) -> list[render.Rendered]:
    root = chart.parent
    return render.render_chart(
        helm,
        chart,
        root=root,
        extra_args=[],
        out_dir=root / "o",
        stage_dir=root / "s",
        **kw,
    )


def _build(root: Path) -> render.Rendered:
    return render.render_kustomization(
        KUSTOMIZE, root, root=root, out_dir=root / "o", stage_dir=root / "s"
    )


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


class TestValueSets:
    def test_no_ci_dir_means_defaults_only(self, tmp_path: Path) -> None:
        assert render.ci_values(_chart(tmp_path)) == []

    def test_only_dash_values_files_count(self, tmp_path: Path) -> None:
        chart = _chart(tmp_path)
        (chart / "ci").mkdir()
        for name in ("b-values.yaml", "a-values.yaml", "README.md", "values.yaml"):
            (chart / "ci" / name).write_text("value: x\n", encoding="utf-8")
        assert [p.name for p in render.ci_values(chart)] == [
            "a-values.yaml",
            "b-values.yaml",
        ]

    def test_iac_helm_keys_become_arguments(self, tmp_path: Path) -> None:
        cfg = CIConfig(
            _raw={"iac": {"helm": {"values": ["v/common.yaml"], "set": {"a.b": 1}}}}
        )
        args = render.helm_value_args(cfg, tmp_path)
        assert args == ["-f", str(tmp_path / "v/common.yaml"), "--set", "a.b=1"]

    def test_set_also_takes_a_list(self, tmp_path: Path) -> None:
        cfg = CIConfig(_raw={"iac": {"helm": {"set": ["x=1"]}}})
        assert render.helm_value_args(cfg, tmp_path) == ["--set", "x=1"]

    def test_nothing_configured_adds_nothing(self, tmp_path: Path) -> None:
        assert render.helm_value_args(CIConfig(_raw={}), tmp_path) == []


class TestDependencies:
    def _with_dep(self, tmp_path: Path) -> Path:
        chart = _chart(tmp_path)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: svc\nversion: 0.1.0\n"
            "dependencies:\n  - name: common\n    version: 1.0.0\n",
            encoding="utf-8",
        )
        return chart

    def test_a_vendored_tgz_satisfies_the_dependency(self, tmp_path: Path) -> None:
        chart = self._with_dep(tmp_path)
        (chart / "charts").mkdir()
        (chart / "charts" / "common-1.0.0.tgz").write_bytes(b"")
        assert render.missing_dependencies(chart) == []

    def test_an_absent_dependency_is_named(self, tmp_path: Path) -> None:
        assert render.missing_dependencies(self._with_dep(tmp_path)) == ["common"]

    def test_build_runs_only_when_something_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_: object) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="kind: X\n", stderr="")

        monkeypatch.setattr(render, "run_cmd", _run)
        monkeypatch.setattr(findings, "run_cmd", _run)
        chart = self._with_dep(tmp_path)
        _render_chart(chart, "helm")
        staged = tmp_path / "s" / chart.name
        assert ["helm", "dependency", "build", str(staged)] in calls
        assert not any(str(chart) in c for c in calls if "dependency" in c)
        template = next(c for c in calls if "template" in c)
        assert str(staged) in template
        assert "--skip-tests" in template
        assert not (tmp_path / "s").exists()

        calls.clear()
        (chart / "charts").mkdir()
        (chart / "charts" / "common-1.0.0.tgz").write_bytes(b"")
        _render_chart(chart, "helm")
        assert not any("dependency" in c for c in calls)


class TestTestHooks:
    def test_test_hook_documents_are_dropped(self) -> None:
        text = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: keep\n"
            "---\napiVersion: v1\nkind: Pod\nmetadata:\n  name: t-abc12\n"
            "  annotations:\n    helm.sh/hook: test-success\n"
            "---\napiVersion: v1\nkind: Job\nmetadata:\n  name: migrate\n"
            "  annotations:\n    helm.sh/hook: pre-install\n"
        )
        kept = render.without_test_hooks(text)
        assert "keep" in kept
        assert "migrate" in kept
        assert "t-abc12" not in kept


class TestStaging:
    def test_kustomization_and_its_references_are_copied(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        (root / "base").mkdir(parents=True)
        (root / "base" / "kustomization.yaml").write_text(
            "resources: [cm.yaml]\n", encoding="utf-8"
        )
        (root / "base" / "cm.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")
        (root / "overlay").mkdir()
        (root / "overlay" / "kustomization.yaml").write_text(
            "resources: [../base]\nhelmCharts: [{name: x, valuesFile: ../v.yaml}]\n",
            encoding="utf-8",
        )
        (root / "v.yaml").write_text("a: 1\n", encoding="utf-8")
        staged = stage_tree(
            root / "overlay",
            root,
            tmp_path / "s",
            kustomization_refs,
            render._STAGE_IGNORE,
        )
        assert staged == tmp_path / "s" / "overlay"
        assert (tmp_path / "s" / "base" / "cm.yaml").is_file()
        assert (tmp_path / "s" / "v.yaml").is_file()


class TestTimeout:
    def test_a_hung_render_is_a_finding_not_a_hang(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _run(cmd: list[str], **kw: Any) -> SimpleNamespace:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

        monkeypatch.setattr(findings, "run_cmd", _run)
        (result,) = _render_chart(_chart(tmp_path), "helm", timeout=5)
        assert result.output is None
        assert result.finding is not None
        assert result.finding.rule == "helm/timeout"


@needs_helm
class TestRealHelm:
    def test_defaults_render_once(self, tmp_path: Path) -> None:
        (result,) = _render_chart(_chart(tmp_path))
        assert result.finding is None
        assert result.output is not None
        assert 'value: "default"' in result.output.read_text(encoding="utf-8")

    def test_one_render_per_ci_values_file(self, tmp_path: Path) -> None:
        chart = _chart(tmp_path)
        (chart / "ci").mkdir()
        (chart / "ci" / "a-values.yaml").write_text("value: a\n", encoding="utf-8")
        (chart / "ci" / "b-values.yaml").write_text("value: b\n", encoding="utf-8")
        renders = _render_chart(chart)
        assert len(renders) == 2
        texts = [r.output.read_text(encoding="utf-8") for r in renders if r.output]
        assert any('value: "a"' in t for t in texts)
        assert any('value: "b"' in t for t in texts)

    @pytest.mark.parametrize(
        ("value", "rule", "rendered", "says"),
        [
            # An unstable render is still schema-validated, so it keeps its output.
            ("{{ randAlphaNum 16 | quote }}", "render-unstable", True, "two renders"),
            ('{{ required "need x" .Values.x }}', "render-failed", False, "need x"),
        ],
    )
    def test_a_bad_template_is_a_finding(
        self, tmp_path: Path, value: str, rule: str, rendered: bool, says: str
    ) -> None:
        template = _CONFIGMAP.replace("{{ .Values.value | quote }}", value)
        (result,) = _render_chart(_chart(tmp_path, template=template))
        assert (result.output is not None) is rendered
        assert result.finding is not None
        assert result.finding.rule == f"helm/{rule}"
        assert says in result.finding.message


@needs_kustomize
class TestRealKustomize:
    def test_builds_a_kustomization(self, tmp_path: Path) -> None:
        (tmp_path / "cm.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", encoding="utf-8"
        )
        (tmp_path / "kustomization.yaml").write_text(
            "resources:\n  - cm.yaml\nnamePrefix: p-\n", encoding="utf-8"
        )
        result = _build(tmp_path)
        assert result.finding is None
        assert result.output is not None
        assert "name: p-x" in result.output.read_text(encoding="utf-8")

    def test_a_missing_resource_is_a_finding(self, tmp_path: Path) -> None:
        (tmp_path / "kustomization.yaml").write_text(
            "resources:\n  - gone.yaml\n", encoding="utf-8"
        )
        result = _build(tmp_path)
        assert result.output is None
        assert result.finding is not None
        assert result.finding.rule == "kustomize/render-failed"
