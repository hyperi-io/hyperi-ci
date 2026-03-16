# Project:   HyperI CI
# File:      tests/unit/test_init.py
# Purpose:   Tests for init command and template rendering
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

from hyperi_ci.init import (
    _detect_python_build_type,
    _detect_rust_workspace,
    _has_releaserc,
    _has_renovate_config,
    _makefile_has_ci_targets,
    _render_hyperi_ci_yaml,
    _render_makefile,
    _render_releaserc,
    _render_renovate_json,
    _render_workflow,
    init_project,
)


class TestRenderTemplates:
    """Template rendering produces valid content."""

    def test_yaml_contains_language(self, tmp_path: Path) -> None:
        content = _render_hyperi_ci_yaml("python", "my-project", tmp_path)
        assert "language: python" in content

    def test_yaml_contains_project_name(self, tmp_path: Path) -> None:
        content = _render_hyperi_ci_yaml("rust", "my-project", tmp_path)
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
        content = _render_workflow("my-project", "python-ci.yml")
        assert "python-ci.yml@" in content
        assert "hyperi-io/hyperi-ci" in content

    def test_workflow_inherits_secrets(self) -> None:
        content = _render_workflow("my-project", "rust-ci.yml")
        assert "secrets: inherit" in content


class TestDetectPythonBuildType:
    """Python build type detection from pyproject.toml."""

    def test_app_with_scripts(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n[project.scripts]\nmycli = 'myapp:main'\n"
        )
        assert _detect_python_build_type(tmp_path) == "app"

    def test_package_without_scripts(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mylib'\n")
        assert _detect_python_build_type(tmp_path) == "package"

    def test_no_pyproject(self, tmp_path: Path) -> None:
        assert _detect_python_build_type(tmp_path) == "package"


class TestDetectRustWorkspace:
    """Rust workspace detection from Cargo.toml."""

    def test_workspace_detected(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[workspace]\nmembers = ['crate-a']\n")
        assert _detect_rust_workspace(tmp_path) is True

    def test_single_crate(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'myapp'\n")
        assert _detect_rust_workspace(tmp_path) is False

    def test_no_cargo_toml(self, tmp_path: Path) -> None:
        assert _detect_rust_workspace(tmp_path) is False


class TestMakefileHasCiTargets:
    """Makefile CI target detection."""

    def test_has_targets(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("quality:\n\techo hi\n")
        assert _makefile_has_ci_targets(tmp_path) is True

    def test_no_targets(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("clean:\n\trm -rf build\n")
        assert _makefile_has_ci_targets(tmp_path) is False

    def test_no_makefile(self, tmp_path: Path) -> None:
        assert _makefile_has_ci_targets(tmp_path) is False


class TestHasReleaserc:
    """Semantic-release config detection."""

    def test_yaml_detected(self, tmp_path: Path) -> None:
        (tmp_path / ".releaserc.yaml").write_text("branches: [main]\n")
        assert _has_releaserc(tmp_path) is True

    def test_js_detected(self, tmp_path: Path) -> None:
        (tmp_path / "release.config.js").write_text("module.exports = {}\n")
        assert _has_releaserc(tmp_path) is True

    def test_none_found(self, tmp_path: Path) -> None:
        assert _has_releaserc(tmp_path) is False


class TestRenderReleaserc:
    """Releaserc rendering."""

    def test_contains_conventional_commits(self) -> None:
        content = _render_releaserc("my-project")
        assert "conventionalcommits" in content

    def test_contains_project_name(self) -> None:
        content = _render_releaserc("my-project")
        assert "my-project" in content


class TestLanguageSpecificConfig:
    """Language-specific config defaults in generated YAML."""

    def test_python_build_type(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n[project.scripts]\ncli = 'app:main'\n"
        )
        content = _render_hyperi_ci_yaml("python", "test", tmp_path)
        assert "type: app" in content

    def test_rust_workspace_config(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[workspace]\nmembers = []\n")
        content = _render_hyperi_ci_yaml("rust", "test", tmp_path)
        assert "workspace:" in content

    def test_golang_targets(self, tmp_path: Path) -> None:
        content = _render_hyperi_ci_yaml("golang", "test", tmp_path)
        assert "linux/amd64" in content

    def test_typescript_package_manager(self, tmp_path: Path) -> None:
        content = _render_hyperi_ci_yaml("typescript", "test", tmp_path)
        assert "package_manager: auto" in content


class TestInitProject:
    """Full init_project integration tests."""

    def test_generates_all_files(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        rc = init_project(tmp_path)
        assert rc == 0
        assert (tmp_path / ".hyperi-ci.yaml").exists()
        assert (tmp_path / "Makefile").exists()
        assert (tmp_path / ".github" / "workflows" / "ci.yml").exists()
        assert (tmp_path / ".releaserc.yaml").exists()

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

    def test_skips_makefile_with_ci_targets(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        existing_makefile = "quality:\n\techo existing\n"
        (tmp_path / "Makefile").write_text(existing_makefile)
        rc = init_project(tmp_path)
        assert rc == 0
        assert (tmp_path / "Makefile").read_text() == existing_makefile

    def test_skips_releaserc_when_exists(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / ".releaserc.yaml").write_text("branches: [main]\n")
        rc = init_project(tmp_path)
        assert rc == 0
        assert (tmp_path / ".releaserc.yaml").read_text() == "branches: [main]\n"

    def test_deprecated_config_does_not_block(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / ".hypersec-ci.yaml").write_text("old: true\n")
        rc = init_project(tmp_path)
        assert rc == 0

    def test_generates_renovate_json(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        rc = init_project(tmp_path)
        assert rc == 0
        assert (tmp_path / "renovate.json").exists()

    def test_skips_renovate_json_when_exists(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        existing = '{"extends": ["local"]}\n'
        (tmp_path / "renovate.json").write_text(existing)
        rc = init_project(tmp_path)
        assert rc == 0
        assert (tmp_path / "renovate.json").read_text() == existing

    def test_skips_renovaterc_json_when_exists(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / ".renovaterc.json").write_text("{}\n")
        rc = init_project(tmp_path)
        assert rc == 0
        assert not (tmp_path / "renovate.json").exists()


class TestHasRenovateConfig:
    """Renovate config detection."""

    def test_renovate_json_detected(self, tmp_path: Path) -> None:
        (tmp_path / "renovate.json").write_text("{}\n")
        assert _has_renovate_config(tmp_path) is True

    def test_renovaterc_detected(self, tmp_path: Path) -> None:
        (tmp_path / ".renovaterc").write_text("{}\n")
        assert _has_renovate_config(tmp_path) is True

    def test_renovaterc_json_detected(self, tmp_path: Path) -> None:
        (tmp_path / ".renovaterc.json").write_text("{}\n")
        assert _has_renovate_config(tmp_path) is True

    def test_none_found(self, tmp_path: Path) -> None:
        assert _has_renovate_config(tmp_path) is False


class TestRenderRenovateJson:
    """Renovate JSON rendering."""

    def test_extends_org_preset(self) -> None:
        content = _render_renovate_json()
        assert "github>hyperi-io/renovate-config" in content

    def test_valid_json(self) -> None:
        import json

        data = json.loads(_render_renovate_json())
        assert "$schema" in data
        assert "extends" in data

    def test_minimal_config(self) -> None:
        import json

        data = json.loads(_render_renovate_json())
        assert len(data) == 2
