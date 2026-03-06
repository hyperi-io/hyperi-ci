# Project:   HyperI CI
# File:      tests/unit/test_migrate.py
# Purpose:   Tests for migrate command
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import subprocess
from pathlib import Path

from hyperi_ci.migrate import (
    _clean_gitmodules,
    _find_old_ci_env_refs,
    _find_old_workflows,
    _has_ci_directory,
    _has_ci_submodule,
    _workflow_references_old_ci,
    migrate_project,
)


def _git_init(path: Path) -> None:
    """Initialise a git repo at path for testing."""
    subprocess.run(
        ["git", "init"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        capture_output=True,
        check=True,
    )


class TestHasCiSubmodule:
    """Detection of ci/ submodule in .gitmodules."""

    def test_submodule_found(self, tmp_path: Path) -> None:
        gitmodules = tmp_path / ".gitmodules"
        gitmodules.write_text(
            '[submodule "ci"]\n'
            "\tpath = ci\n"
            "\turl = https://github.com/hyperi-io/ci.git\n"
        )
        assert _has_ci_submodule(tmp_path) is True

    def test_no_gitmodules(self, tmp_path: Path) -> None:
        assert _has_ci_submodule(tmp_path) is False

    def test_other_submodules_only(self, tmp_path: Path) -> None:
        gitmodules = tmp_path / ".gitmodules"
        gitmodules.write_text(
            '[submodule "ai"]\n'
            "\tpath = ai\n"
            "\turl = https://github.com/hyperi-io/ai.git\n"
        )
        assert _has_ci_submodule(tmp_path) is False

    def test_submodule_among_others(self, tmp_path: Path) -> None:
        gitmodules = tmp_path / ".gitmodules"
        gitmodules.write_text(
            '[submodule "ci"]\n'
            "\tpath = ci\n"
            "\turl = https://github.com/hyperi-io/ci.git\n"
            '[submodule "ai"]\n'
            "\tpath = ai\n"
            "\turl = https://github.com/hyperi-io/ai.git\n"
        )
        assert _has_ci_submodule(tmp_path) is True


class TestHasCiDirectory:
    """Detection of ci/ directory."""

    def test_directory_exists(self, tmp_path: Path) -> None:
        (tmp_path / "ci").mkdir()
        assert _has_ci_directory(tmp_path) is True

    def test_no_directory(self, tmp_path: Path) -> None:
        assert _has_ci_directory(tmp_path) is False

    def test_file_not_directory(self, tmp_path: Path) -> None:
        (tmp_path / "ci").write_text("not a dir")
        assert _has_ci_directory(tmp_path) is False


class TestWorkflowReferencesOldCi:
    """Detection of old CI references in workflow files."""

    def test_references_ci_actions(self, tmp_path: Path) -> None:
        wf = tmp_path / "ci.yml"
        wf.write_text("uses: ./ci/actions/jobs/quality\n")
        assert _workflow_references_old_ci(wf) is True

    def test_references_ci_scripts(self, tmp_path: Path) -> None:
        wf = tmp_path / "ci.yml"
        wf.write_text("scripts-path: ./ci/scripts\n")
        assert _workflow_references_old_ci(wf) is True

    def test_new_style_workflow(self, tmp_path: Path) -> None:
        wf = tmp_path / "ci.yml"
        wf.write_text("uses: hyperi-io/hyperi-ci/.github/workflows/rust-ci.yml@main\n")
        assert _workflow_references_old_ci(wf) is False

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        wf = tmp_path / "missing.yml"
        assert _workflow_references_old_ci(wf) is False


class TestFindOldWorkflows:
    """Discovery of old-style workflow files."""

    def test_finds_old_workflows(self, tmp_path: Path) -> None:
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("uses: ./ci/actions/jobs/quality\n")
        (wf_dir / "publish.yml").write_text("uses: ./ci/actions/jobs/publish\n")
        (wf_dir / "other.yml").write_text("uses: actions/checkout@v4\n")
        result = _find_old_workflows(tmp_path)
        names = {f.name for f in result}
        assert names == {"ci.yml", "publish.yml"}

    def test_no_workflows_dir(self, tmp_path: Path) -> None:
        assert _find_old_workflows(tmp_path) == []

    def test_yaml_extension(self, tmp_path: Path) -> None:
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yaml").write_text("uses: ./ci/actions/jobs/test\n")
        result = _find_old_workflows(tmp_path)
        assert len(result) == 1
        assert result[0].name == "ci.yaml"


class TestCleanGitmodules:
    """Cleaning ci entry from .gitmodules."""

    def test_removes_ci_entry_preserves_others(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        gitmodules = tmp_path / ".gitmodules"
        gitmodules.write_text(
            '[submodule "ci"]\n'
            "\tpath = ci\n"
            "\turl = https://github.com/hyperi-io/ci.git\n"
            "\tfetchRecurseSubmodules = true\n"
            "\tupdate = rebase\n"
            '[submodule "ai"]\n'
            "\tpath = ai\n"
            "\turl = https://github.com/hyperi-io/ai.git\n"
        )
        _clean_gitmodules(tmp_path)
        content = gitmodules.read_text()
        assert "ci" not in content.split('"ai"')[0]
        assert '[submodule "ai"]' in content
        assert "path = ai" in content

    def test_removes_file_when_only_ci(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        gitmodules = tmp_path / ".gitmodules"
        gitmodules.write_text(
            '[submodule "ci"]\n'
            "\tpath = ci\n"
            "\turl = https://github.com/hyperi-io/ci.git\n"
        )
        # Need to stage gitmodules for git rm to work
        subprocess.run(
            ["git", "add", ".gitmodules"],
            cwd=tmp_path,
            capture_output=True,
        )
        _clean_gitmodules(tmp_path)
        assert not gitmodules.exists()

    def test_noop_when_no_ci_entry(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        gitmodules = tmp_path / ".gitmodules"
        original = (
            '[submodule "ai"]\n'
            "\tpath = ai\n"
            "\turl = https://github.com/hyperi-io/ai.git\n"
        )
        gitmodules.write_text(original)
        _clean_gitmodules(tmp_path)
        assert gitmodules.read_text() == original

    def test_noop_when_no_gitmodules(self, tmp_path: Path) -> None:
        _clean_gitmodules(tmp_path)
        assert not (tmp_path / ".gitmodules").exists()


class TestFindOldCiEnvRefs:
    """Detection of old CI environment variable references."""

    def test_finds_artifactory_refs(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("ARTIFACTORY_CI_TOKEN=foo\n")
        warnings = _find_old_ci_env_refs(tmp_path)
        assert len(warnings) == 1
        assert "Makefile" in warnings[0]

    def test_skips_workflow_dir(self, tmp_path: Path) -> None:
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("ARTIFACTORY_CI_TOKEN\n")
        warnings = _find_old_ci_env_refs(tmp_path)
        assert len(warnings) == 0

    def test_skips_git_dir(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git" / "config"
        git_dir.parent.mkdir(parents=True)
        git_dir.write_text("ARTIFACTORY_CI_TOKEN\n")
        warnings = _find_old_ci_env_refs(tmp_path)
        assert len(warnings) == 0


class TestMigrateProject:
    """Full migration integration tests."""

    def _setup_old_ci_project(
        self,
        tmp_path: Path,
        *,
        with_submodule_entry: bool = True,
        with_config: bool = False,
        language_file: str = "Cargo.toml",
    ) -> None:
        """Create a project mimicking the old CI layout."""
        _git_init(tmp_path)

        # Language marker
        (tmp_path / language_file).write_text("[package]\nname = 'test'\n")

        # ci/ directory (simulates submodule checkout)
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "README.md").write_text("old ci\n")

        # .gitmodules
        if with_submodule_entry:
            (tmp_path / ".gitmodules").write_text(
                '[submodule "ci"]\n'
                "\tpath = ci\n"
                "\turl = https://github.com/hyperi-io/ci.git\n"
            )

        # Old workflow
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(
            "name: CI\n"
            "jobs:\n"
            "  quality:\n"
            "    steps:\n"
            "      - uses: ./ci/actions/jobs/quality\n"
        )
        (wf_dir / "publish.yml").write_text(
            "name: Publish\n"
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - uses: ./ci/actions/jobs/build\n"
        )

        # Existing config
        if with_config:
            (tmp_path / ".hyperi-ci.yaml").write_text(
                "language: rust\nrunners:\n  default: arc-runner-16cpu\n"
            )

        # Initial commit so git operations work
        subprocess.run(
            ["git", "add", "-A"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmp_path,
            capture_output=True,
        )

    def test_removes_ci_directory(self, tmp_path: Path) -> None:
        self._setup_old_ci_project(tmp_path, with_submodule_entry=False)
        rc = migrate_project(tmp_path)
        assert rc == 0
        assert not (tmp_path / "ci").exists()

    def test_removes_old_workflows(self, tmp_path: Path) -> None:
        self._setup_old_ci_project(tmp_path, with_submodule_entry=False)
        rc = migrate_project(tmp_path)
        assert rc == 0
        wf_dir = tmp_path / ".github" / "workflows"
        assert not (wf_dir / "publish.yml").exists()

    def test_generates_new_workflow(self, tmp_path: Path) -> None:
        self._setup_old_ci_project(tmp_path, with_submodule_entry=False)
        rc = migrate_project(tmp_path)
        assert rc == 0
        wf = tmp_path / ".github" / "workflows" / "ci.yml"
        assert wf.exists()
        content = wf.read_text()
        assert "hyperi-io/hyperi-ci" in content
        assert "rust-ci.yml" in content

    def test_preserves_existing_config(self, tmp_path: Path) -> None:
        self._setup_old_ci_project(
            tmp_path,
            with_submodule_entry=False,
            with_config=True,
        )
        rc = migrate_project(tmp_path)
        assert rc == 0
        content = (tmp_path / ".hyperi-ci.yaml").read_text()
        assert "arc-runner-16cpu" in content

    def test_generates_config_when_missing(self, tmp_path: Path) -> None:
        self._setup_old_ci_project(tmp_path, with_submodule_entry=False)
        rc = migrate_project(tmp_path)
        assert rc == 0
        config = tmp_path / ".hyperi-ci.yaml"
        assert config.exists()
        assert "language: rust" in config.read_text()

    def test_nothing_to_migrate(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        rc = migrate_project(tmp_path)
        assert rc == 0

    def test_not_git_repo(self, tmp_path: Path) -> None:
        rc = migrate_project(tmp_path)
        assert rc == 1

    def test_dry_run_no_changes(self, tmp_path: Path) -> None:
        self._setup_old_ci_project(tmp_path, with_submodule_entry=False)
        rc = migrate_project(tmp_path, dry_run=True)
        assert rc == 0
        # ci/ should still exist
        assert (tmp_path / "ci").is_dir()
        # Old workflow should still exist
        wf = tmp_path / ".github" / "workflows" / "ci.yml"
        assert "./ci/actions/" in wf.read_text()

    def test_python_project(self, tmp_path: Path) -> None:
        self._setup_old_ci_project(
            tmp_path,
            with_submodule_entry=False,
            language_file="pyproject.toml",
        )
        # pyproject.toml needs valid content for detection
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        rc = migrate_project(tmp_path)
        assert rc == 0
        wf = tmp_path / ".github" / "workflows" / "ci.yml"
        assert "python-ci.yml" in wf.read_text()

    def test_generates_releaserc(self, tmp_path: Path) -> None:
        self._setup_old_ci_project(tmp_path, with_submodule_entry=False)
        rc = migrate_project(tmp_path)
        assert rc == 0
        assert (tmp_path / ".releaserc.yaml").exists()

    def test_preserves_existing_releaserc(self, tmp_path: Path) -> None:
        self._setup_old_ci_project(tmp_path, with_submodule_entry=False)
        existing = "branches: [main]\nplugins: []\n"
        (tmp_path / ".releaserc.yaml").write_text(existing)
        subprocess.run(
            ["git", "add", "-A"],
            cwd=tmp_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add releaserc"],
            cwd=tmp_path,
            capture_output=True,
        )
        rc = migrate_project(tmp_path)
        assert rc == 0
        assert (tmp_path / ".releaserc.yaml").read_text() == existing
