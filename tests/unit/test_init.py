# Project:   HyperI CI
# File:      tests/unit/test_init.py
# Purpose:   Tests for init command and template rendering
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

from hyperi_ci.init import (
    _detect_python_build_type,
    _detect_rust_workspace,
    _has_releaserc,
    _makefile_has_ci_targets,
    _render_contributing,
    _render_hyperi_ci_yaml,
    _render_makefile,
    _render_releaserc,
    _render_workflow,
    detect_license,
    init_project,
)


class TestLicenseConfig:
    """`.hyperi-ci.yaml` can declare the licence; declaration wins over scan."""

    def test_yaml_declaration_overrides_detection(self, tmp_path: Path) -> None:
        # A LICENSE file says Apache, but .hyperi-ci.yaml declares BUSL-1.1.
        (tmp_path / "LICENSE").write_text("Apache License\nLicensed under the Apache")
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "language: python\nlicense: BUSL-1.1\n"
        )
        assert detect_license(tmp_path) == "BUSL-1.1"

    def test_yaml_declaration_used_for_any_id(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("language: rust\nlicense: MIT\n")
        assert detect_license(tmp_path) == "MIT"

    def test_falls_back_to_scan_without_declaration(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("language: python\n")
        (tmp_path / "LICENSE").write_text("MIT License\nPermission is hereby granted")
        assert detect_license(tmp_path) == "MIT"

    def test_busl_marker_matches_full_name(self, tmp_path: Path) -> None:
        # The canonical LICENSE text reads "Business Source License 1.1".
        (tmp_path / "LICENSE").write_text("Business Source License 1.1\n\nParameters\n")
        assert detect_license(tmp_path) == "BUSL-1.1"

    def test_default_is_busl(self, tmp_path: Path) -> None:
        assert detect_license(tmp_path) == "BUSL-1.1"

    def test_scaffold_includes_license_key(self, tmp_path: Path) -> None:
        content = _render_hyperi_ci_yaml("python", "p", tmp_path)
        assert "license: BUSL-1.1" in content

    def test_scaffold_publish_target_is_oss(self, tmp_path: Path) -> None:
        # JFrog removed in v2.1.4 — new projects scaffold to oss, not internal.
        content = _render_hyperi_ci_yaml("python", "p", tmp_path)
        assert "target: oss" in content
        assert "target: internal" not in content


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

    def test_workflow_scaffolds_from_head_dispatch_inputs(self) -> None:
        # issue #35: `hyperi-ci publish` dispatches from-head=true + bump=...;
        # the scaffolded ci.yml must expose those workflow_dispatch inputs and
        # forward them to the language workflow, else the dispatch errors.
        for workflow_file in ("python-ci.yml", "rust-ci.yml", "ts-ci.yml", "go-ci.yml"):
            content = _render_workflow("my-project", workflow_file)
            # Inputs declared
            assert "from-head:" in content, f"{workflow_file}: missing from-head input"
            assert "bump:" in content, f"{workflow_file}: missing bump input"
            # Tag relaxed to optional (else gh workflow run errors on no-tag dispatch)
            assert "required: true" not in content, (
                f"{workflow_file}: tag must be optional for from-head dispatch"
            )
            # Forwarded to the language workflow
            assert "from-head: ${{ inputs.from-head" in content
            assert "bump: ${{ inputs.bump" in content

    def test_workflow_dispatch_accepts_tag_input(self) -> None:
        # `hyperi-ci publish vX.Y.Z` calls `gh workflow run ci.yml -f
        # tag=vX.Y.Z`. Without a `tag` input on workflow_dispatch the
        # GitHub API returns 422 "Unexpected inputs provided" and
        # publish silently fails. Every scaffolded ci.yml must accept
        # the tag input AND forward it to the language workflow.
        for workflow_file in ("python-ci.yml", "rust-ci.yml", "ts-ci.yml", "go-ci.yml"):
            content = _render_workflow("my-project", workflow_file)
            assert "workflow_dispatch:" in content
            assert "inputs:" in content, (
                f"{workflow_file}: missing workflow_dispatch inputs"
            )
            assert "tag:" in content, f"{workflow_file}: missing tag input"
            assert "tag: ${{ inputs.tag" in content, (
                f"{workflow_file}: tag input not forwarded to language workflow"
            )


class TestRenderContributing:
    """CONTRIBUTING.md template tells external contributors they don't
    need any HyperI tooling, and tells maintainers exactly what to set
    up to get the strict workflow.
    """

    def test_includes_project_name(self) -> None:
        content = _render_contributing("my-project")
        assert "my-project" in content

    def test_has_two_audiences(self) -> None:
        # The whole point of this template: separate paths for the
        # two reader profiles.
        content = _render_contributing("my-project")
        assert "External contributors" in content
        assert "Maintainers" in content

    def test_external_contributors_do_not_need_hyperi_ci(self) -> None:
        # If we ever drift back to "everyone must install hyperi-ci",
        # this test catches it.
        content = _render_contributing("my-project")
        assert (
            "do NOT need to install" in content or "do not need to install" in content
        )

    def test_maintainers_install_hyperi_ci_via_uv_tool(self) -> None:
        # The maintainer section must show the install path so newcomers
        # to the maintainer role have the bootstrap right there.
        content = _render_contributing("my-project")
        assert "uv tool install hyperi-ci" in content

    def test_maintainers_activate_hooks(self) -> None:
        # The hooks are opt-in. Maintainer section must document the
        # activation command.
        content = _render_contributing("my-project")
        assert "git config core.hooksPath .githooks" in content

    def test_documents_fork_pr_skipped_ci(self) -> None:
        # The fork-PR gate in the workflows means contributors see
        # green checks for non-runs. We surface that explicitly so
        # they aren't misled.
        content = _render_contributing("my-project")
        # Substring of the relevant explanation, robust to wording
        # tweaks but anchored on the key concept.
        assert "skipped" in content.lower()

    def test_documents_hyperi_ci_push_for_maintainers(self) -> None:
        content = _render_contributing("my-project")
        assert "hyperi-ci push" in content


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

    def test_branches_main_only(self) -> None:
        content = _render_releaserc("my-project")
        assert "prerelease" not in content
        assert "- main" in content

    def test_no_github_plugin(self) -> None:
        content = _render_releaserc("my-project")
        assert "@semantic-release/github" not in content

    def test_has_all_commit_types(self) -> None:
        content = _render_releaserc("my-project")
        for t in [
            "cleanup",
            "data",
            "debt",
            "design",
            "infra",
            "meta",
            "ops",
            "review",
            "spike",
            "ui",
            "hotfix",
            "security",
        ]:
            assert t in content, f"Missing commit type: {t}"


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

    def test_creates_commit_hook(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        init_project(tmp_path)
        hook = tmp_path / ".githooks" / "commit-msg"
        assert hook.exists()
        assert hook.stat().st_mode & 0o111  # executable
        content = hook.read_text()
        assert "check-commit" in content

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
