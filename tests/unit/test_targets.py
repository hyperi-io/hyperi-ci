# Project:   HyperI CI
# File:      tests/unit/test_targets.py
# Purpose:   Tests for lint-target discovery (Dockerfiles)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.targets.discover_dockerfiles."""

from __future__ import annotations

from pathlib import Path

from hyperi_ci.quality.targets import (
    discover_dockerfiles,
    discover_helm_charts,
    discover_manifests,
)


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("FROM scratch\n", encoding="utf-8")


class TestDiscoverDockerfiles:
    def test_matches_all_forms(self, tmp_path: Path) -> None:
        _touch(tmp_path / "Dockerfile")
        _touch(tmp_path / "Dockerfile.dev")
        _touch(tmp_path / "app.Dockerfile")
        _touch(tmp_path / "Containerfile")
        _touch(tmp_path / "svc" / "Dockerfile")
        found = {p.name for p in discover_dockerfiles(tmp_path)}
        assert found == {
            "Dockerfile",
            "Dockerfile.dev",
            "app.Dockerfile",
            "Containerfile",
        }

    def test_ignores_dockerignore(self, tmp_path: Path) -> None:
        _touch(tmp_path / "Dockerfile")
        (tmp_path / ".dockerignore").write_text("node_modules\n", encoding="utf-8")
        found = [p.name for p in discover_dockerfiles(tmp_path)]
        assert ".dockerignore" not in found
        assert "Dockerfile" in found

    def test_prunes_default_dirs(self, tmp_path: Path) -> None:
        # A Dockerfile inside an always-pruned dir must not be discovered - this
        # is the .worktrees duplicate-tree double-count guard.
        _touch(tmp_path / "Dockerfile")
        _touch(tmp_path / ".worktrees" / "branch" / "Dockerfile")
        _touch(tmp_path / "node_modules" / "pkg" / "Dockerfile")
        found = [p for p in discover_dockerfiles(tmp_path)]
        assert len(found) == 1
        assert found[0] == tmp_path / "Dockerfile"

    def test_respects_extra_excludes(self, tmp_path: Path) -> None:
        _touch(tmp_path / "Dockerfile")
        _touch(tmp_path / "third_party" / "Dockerfile")
        found = discover_dockerfiles(tmp_path, exclude_dirs=["third_party"])
        assert [p.parent.name for p in found] == [tmp_path.name]

    def test_empty_when_none(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
        assert discover_dockerfiles(tmp_path) == []

    def test_sorted_output(self, tmp_path: Path) -> None:
        _touch(tmp_path / "b.Dockerfile")
        _touch(tmp_path / "a.Dockerfile")
        found = discover_dockerfiles(tmp_path)
        assert found == sorted(found)


def _chart(dir_: Path, *, library: bool = False) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    body = "apiVersion: v2\nname: c\nversion: 0.1.0\n"
    if library:
        body += "type: library\n"
    (dir_ / "Chart.yaml").write_text(body, encoding="utf-8")


class TestDiscoverHelmCharts:
    def test_finds_top_level_skips_library_and_subcharts(self, tmp_path: Path) -> None:
        _chart(tmp_path / "charts" / "svc")
        _chart(tmp_path / "charts" / "svc" / "charts" / "sub")  # subchart
        _chart(tmp_path / "library" / "common", library=True)  # library type
        found = discover_helm_charts(tmp_path)
        assert found == [tmp_path / "charts" / "svc"]

    def test_prunes_worktrees(self, tmp_path: Path) -> None:
        _chart(tmp_path / "charts" / "svc")
        _chart(tmp_path / ".worktrees" / "branch" / "charts" / "svc")
        found = discover_helm_charts(tmp_path)
        assert found == [tmp_path / "charts" / "svc"]


class TestDiscoverManifests:
    def test_only_apiversion_kind_docs(self, tmp_path: Path) -> None:
        # deploy.yaml has apiVersion+kind; values.yaml does not -> only deploy.
        (tmp_path / "deploy.yaml").write_text(
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\n",
            encoding="utf-8",
        )
        (tmp_path / "values.yaml").write_text(
            "replicas: 3\nimage: x\n", encoding="utf-8"
        )
        found = [p.name for p in discover_manifests(tmp_path)]
        assert found == ["deploy.yaml"]

    def test_skips_chart_content(self, tmp_path: Path) -> None:
        # A chart's templates/ + values.yaml must NOT be picked up as plain
        # manifests - they are rendered separately. Even a template that happens
        # to parse as YAML is excluded because it lives inside a chart.
        chart = tmp_path / "charts" / "svc"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: svc\n", encoding="utf-8"
        )
        (chart / "values.yaml").write_text("replicas: 1\n", encoding="utf-8")
        (chart / "templates" / "dep.yaml").write_text(
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\n",
            encoding="utf-8",
        )
        assert discover_manifests(tmp_path) == []

    def test_finds_manifest_outside_chart(self, tmp_path: Path) -> None:
        # A plain manifest not inside a chart IS discovered.
        (tmp_path / "argocd").mkdir()
        (tmp_path / "argocd" / "app.yaml").write_text(
            "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: a\n",
            encoding="utf-8",
        )
        assert [p.name for p in discover_manifests(tmp_path)] == ["app.yaml"]

    def test_multi_doc_file_counts(self, tmp_path: Path) -> None:
        (tmp_path / "multi.yaml").write_text(
            "replicas: 1\n---\napiVersion: v1\nkind: Service\nmetadata:\n  name: s\n",
            encoding="utf-8",
        )
        assert [p.name for p in discover_manifests(tmp_path)] == ["multi.yaml"]

    def test_repo_is_chart_at_root_skips_everything(self, tmp_path: Path) -> None:
        # When the discovery root IS a chart, every file is chart content ->
        # discover_manifests returns nothing (it is all rendered separately).
        (tmp_path / "Chart.yaml").write_text(
            "apiVersion: v2\nname: c\n", encoding="utf-8"
        )
        (tmp_path / "manifest.yaml").write_text(
            "apiVersion: v1\nkind: Service\nmetadata:\n  name: s\n", encoding="utf-8"
        )
        assert discover_manifests(tmp_path) == []

    def test_chart_at_root_is_discovered_as_chart(self, tmp_path: Path) -> None:
        _chart(tmp_path)
        assert discover_helm_charts(tmp_path) == [tmp_path]
