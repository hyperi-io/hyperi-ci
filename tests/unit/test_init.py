# Project:   HyperI CI
# File:      tests/unit/test_init.py
# Purpose:   Tests for init command and template rendering
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

from hyperi_ci.init import (
    _render_hyperi_ci_yaml,
    _render_makefile,
    _render_workflow,
    init_project,
)


class TestRenderTemplates:
    """Template rendering produces valid content."""

    def test_yaml_contains_language(self) -> None:
        content = _render_hyperi_ci_yaml("python", "my-project")
        assert "language: python" in content

    def test_yaml_contains_project_name(self) -> None:
        content = _render_hyperi_ci_yaml("rust", "my-project")
        assert "my-project" in content

    def test_makefile_contains_targets(self) -> None:
        content = _render_makefile("my-project")
        assert "quality:" in content
        assert "test:" in content
        assert "build:" in content
        assert "hyperi-ci run quality" in content
        assert "hyperi-ci run test" in content
        assert "hyperi-ci run build" in content

    def test_makefile_has_phony(self) -> None:
        content = _render_makefile("my-project")
        assert ".PHONY:" in content

    def test_workflow_references_correct_reusable(self) -> None:
        content = _render_workflow("python", "my-project", "python-ci.yml")
        assert "python-ci.yml@" in content
        assert "hyperi-io/hyperi-ci" in content

    def test_workflow_inherits_secrets(self) -> None:
        content = _render_workflow("rust", "my-project", "rust-ci.yml")
        assert "secrets: inherit" in content


class TestInitProject:
    """Full init_project integration tests."""

    def test_generates_all_files(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        rc = init_project(tmp_path)
        assert rc == 0
        assert (tmp_path / ".hyperi-ci.yaml").exists()
        assert (tmp_path / "Makefile").exists()
        assert (tmp_path / ".github" / "workflows" / "ci.yml").exists()

    def test_yaml_has_correct_language(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        init_project(tmp_path)
        content = (tmp_path / ".hyperi-ci.yaml").read_text()
        assert "language: rust" in content

    def test_skips_existing_without_force(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        existing = "existing content"
        (tmp_path / ".hyperi-ci.yaml").write_text(existing)
        rc = init_project(tmp_path)
        assert rc == 0
        assert (tmp_path / ".hyperi-ci.yaml").read_text() == existing

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / ".hyperi-ci.yaml").write_text("old content")
        rc = init_project(tmp_path, force=True)
        assert rc == 0
        content = (tmp_path / ".hyperi-ci.yaml").read_text()
        assert "language: python" in content

    def test_language_override(self, tmp_path: Path) -> None:
        rc = init_project(tmp_path, language="rust")
        assert rc == 0
        content = (tmp_path / ".hyperi-ci.yaml").read_text()
        assert "language: rust" in content

    def test_no_language_fails(self, tmp_path: Path) -> None:
        rc = init_project(tmp_path)
        assert rc == 1

    def test_creates_github_workflows_dir(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example\n")
        rc = init_project(tmp_path)
        assert rc == 0
        assert (tmp_path / ".github" / "workflows").is_dir()

    def test_workflow_references_go_template(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example\n")
        init_project(tmp_path)
        content = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
        assert "go-ci.yml" in content

    def test_typescript_workflow(self, tmp_path: Path) -> None:
        (tmp_path / "tsconfig.json").write_text("{}\n")
        init_project(tmp_path)
        content = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
        assert "ts-ci.yml" in content
