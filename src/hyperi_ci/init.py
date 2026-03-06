# Project:   HyperI CI
# File:      src/hyperi_ci/init.py
# Purpose:   Project scaffolding — generates config, Makefile, and workflow
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Project initialisation for hyperi-ci.

Generates the files consumer projects need for CI integration.
Existing-project-aware: detects deprecated configs, existing Makefiles
with CI targets, and generates language-appropriate defaults.

Generated files:
  - .hyperi-ci.yaml (CI configuration, language-specific defaults)
  - Makefile (quality/test/build targets — skipped if existing)
  - .github/workflows/ci.yml (reusable workflow caller)
  - .releaserc.yaml (semantic-release config)
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.detect import detect_language

_CI_REPO = "hyperi-io/hyperi-ci"
_WORKFLOW_REF = "main"

_LANGUAGE_WORKFLOW_MAP: dict[str, str] = {
    "python": "python-ci.yml",
    "rust": "rust-ci.yml",
    "typescript": "ts-ci.yml",
    "golang": "go-ci.yml",
}

_DEPRECATED_CONFIG_NAMES = (
    ".hypersec-ci.yaml",
    ".hypersec-ci.yml",
)


def _detect_python_build_type(project_dir: Path) -> str:
    """Detect whether a Python project is an app or a package.

    Checks pyproject.toml for entry points or scripts which indicate
    an application (vs a library/package).

    Args:
        project_dir: Project root directory.

    Returns:
        "app" if entry points found, "package" otherwise.
    """
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        return "package"

    content = pyproject.read_text()
    app_markers = (
        "[project.scripts]",
        "[project.gui-scripts]",
        "entry_points",
        "[tool.poetry.scripts]",
    )
    if any(marker in content for marker in app_markers):
        return "app"
    return "package"


def _detect_rust_workspace(project_dir: Path) -> bool:
    """Detect if a Rust project is a workspace.

    Args:
        project_dir: Project root directory.

    Returns:
        True if Cargo.toml contains [workspace].
    """
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.exists():
        return False
    return "[workspace]" in cargo_toml.read_text()


def _render_hyperi_ci_yaml(
    language: str,
    project_name: str,
    project_dir: Path,
) -> str:
    """Render .hyperi-ci.yaml with language-specific defaults."""
    config: dict = {
        "language": language,
        "quality": {"enabled": True},
        "test": {"enabled": True},
        "build": {"enabled": True, "strategies": ["native"]},
        "publish": {"enabled": True, "target": "internal"},
    }

    if language == "python":
        build_type = _detect_python_build_type(project_dir)
        config["build"]["type"] = build_type

    elif language == "rust":
        config["test"]["coverage"] = False
        config["build"]["rust"] = {
            "features": "all",
            "targets": [],
        }
        if _detect_rust_workspace(project_dir):
            config["workspace"] = {"enabled": "auto"}

    elif language == "golang":
        config["test"]["coverage"] = True
        config["golang"] = {
            "targets": [
                "linux/amd64",
                "linux/arm64",
                "darwin/amd64",
                "darwin/arm64",
            ],
            "cgo": False,
        }

    elif language == "typescript":
        config["test"]["coverage"] = True
        config["typescript"] = {"package_manager": "auto"}

    header = (
        f"# Project:   {project_name}\n"
        "# File:      .hyperi-ci.yaml\n"
        "# Purpose:   HyperI CI configuration\n"
        "#\n"
        "# License:   Proprietary — HYPERI PTY LIMITED\n"
        "# Copyright: (c) 2026 HYPERI PTY LIMITED\n"
        "#\n"
        "# Override defaults with HYPERCI_* env vars.\n"
        "# Reference: config/defaults.yaml in hyperi-ci.\n"
        "\n"
    )
    return header + yaml.dump(
        config,
        default_flow_style=False,
        sort_keys=False,
    )


def _render_makefile(project_name: str) -> str:
    """Render Makefile content with CI targets."""
    return (
        f"# Project:   {project_name}\n"
        "# File:      Makefile\n"
        "# Purpose:   CI targets wrapping hyperi-ci\n"
        "#\n"
        "# License:   Proprietary — HYPERI PTY LIMITED\n"
        "# Copyright: (c) 2026 HYPERI PTY LIMITED\n"
        "\n"
        ".PHONY: quality test build\n"
        "\n"
        "quality:\n"
        "\thyperi-ci run quality\n"
        "\n"
        "test:\n"
        "\thyperi-ci run test\n"
        "\n"
        "build:\n"
        "\thyperi-ci run build\n"
    )


def _render_workflow(
    project_name: str,
    workflow_file: str,
) -> str:
    """Render consumer .github/workflows/ci.yml content."""
    return (
        f"# Project:   {project_name}\n"
        "# File:      .github/workflows/ci.yml\n"
        "# Purpose:   CI pipeline\n"
        "#\n"
        "# License:   Proprietary — HYPERI PTY LIMITED\n"
        "# Copyright: (c) 2026 HYPERI PTY LIMITED\n"
        "\n"
        "name: CI\n"
        "\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "\n"
        "jobs:\n"
        "  ci:\n"
        f"    uses: {_CI_REPO}/.github/workflows/"
        f"{workflow_file}@{_WORKFLOW_REF}\n"
        "    secrets: inherit\n"
    )


def _render_releaserc(project_name: str) -> str:
    """Render .releaserc.yaml for semantic-release."""
    config = {
        "branches": ["main"],
        "tagFormat": "v${version}",
        "plugins": [
            [
                "@semantic-release/commit-analyzer",
                {
                    "preset": "conventionalcommits",
                    "releaseRules": [
                        {"type": "feat", "release": "minor"},
                        {"type": "fix", "release": "patch"},
                        {"type": "perf", "release": "patch"},
                        {"type": "sec", "release": "patch"},
                        {"type": "docs", "release": False},
                        {"type": "test", "release": False},
                        {"type": "refactor", "release": False},
                        {"type": "chore", "release": False},
                        {"type": "ci", "release": False},
                    ],
                },
            ],
            "@semantic-release/release-notes-generator",
            [
                "@semantic-release/changelog",
                {"changelogFile": "CHANGELOG.md"},
            ],
            [
                "@semantic-release/git",
                {
                    "assets": ["CHANGELOG.md", "VERSION"],
                    "message": (
                        "chore: version ${nextRelease.version}"
                        " [skip ci]"
                    ),
                },
            ],
            "@semantic-release/github",
        ],
    }

    header = (
        f"# Project:   {project_name}\n"
        "# File:      .releaserc.yaml\n"
        "# Purpose:   Semantic release configuration\n"
        "#\n"
        "# License:   Proprietary — HYPERI PTY LIMITED\n"
        "# Copyright: (c) 2026 HYPERI PTY LIMITED\n"
        "\n"
    )
    return header + yaml.dump(
        config,
        default_flow_style=False,
        sort_keys=False,
    )


def _write_file(path: Path, content: str, *, force: bool) -> bool:
    """Write a file, respecting the force flag.

    Args:
        path: Destination file path.
        content: File content to write.
        force: Overwrite if file exists.

    Returns:
        True if the file was written.
    """
    if path.exists() and not force:
        warn(
            f"  Skipped {path.name}"
            " (exists, use --force to overwrite)"
        )
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    success(f"  Created {path.name}")
    return True


def _check_deprecated_config(project_dir: Path) -> None:
    """Warn about deprecated config file names."""
    for name in _DEPRECATED_CONFIG_NAMES:
        deprecated = project_dir / name
        if deprecated.exists():
            warn(f"  Found deprecated {name}")
            info("  Rename to .hyperi-ci.yaml")


def _makefile_has_ci_targets(project_dir: Path) -> bool:
    """Check if an existing Makefile already has CI targets.

    Args:
        project_dir: Project root directory.

    Returns:
        True if Makefile has quality/test/build targets.
    """
    makefile = project_dir / "Makefile"
    if not makefile.exists():
        return False

    content = makefile.read_text()
    ci_targets = ["quality:", "test:", "build:"]
    return any(target in content for target in ci_targets)


def _has_releaserc(project_dir: Path) -> bool:
    """Check if semantic-release config already exists."""
    candidates = [
        ".releaserc.yaml",
        ".releaserc.yml",
        ".releaserc.json",
        ".releaserc.js",
        "release.config.js",
        "release.config.cjs",
    ]
    return any((project_dir / name).exists() for name in candidates)


def init_project(
    project_dir: Path,
    *,
    language: str | None = None,
    force: bool = False,
) -> int:
    """Initialise a consumer project for hyperi-ci.

    Existing-project-aware: detects deprecated configs, skips
    Makefiles that already have CI targets, and generates
    language-specific defaults.

    Args:
        project_dir: Project root directory.
        language: Override detected language.
        force: Overwrite existing files.

    Returns:
        Exit code (0 = success).
    """
    project_dir = project_dir.resolve()
    project_name = project_dir.name

    _check_deprecated_config(project_dir)

    detected = language or detect_language(project_dir)
    if not detected:
        error("Could not detect project language")
        info("Use --language: python, rust, typescript, golang")
        return 1

    info(f"Initialising {project_name} as {detected} project")

    workflow_file = _LANGUAGE_WORKFLOW_MAP.get(detected)
    if not workflow_file:
        warn(f"No reusable workflow for {detected}")
        warn("Generating config only")

    files_written = 0

    config_content = _render_hyperi_ci_yaml(
        detected, project_name, project_dir,
    )
    config_path = project_dir / ".hyperi-ci.yaml"
    if _write_file(config_path, config_content, force=force):
        files_written += 1

    if _makefile_has_ci_targets(project_dir) and not force:
        info("  Skipped Makefile (already has CI targets)")
    else:
        makefile_content = _render_makefile(project_name)
        makefile_path = project_dir / "Makefile"
        if _write_file(
            makefile_path, makefile_content, force=force,
        ):
            files_written += 1

    if workflow_file:
        workflow_content = _render_workflow(
            project_name, workflow_file,
        )
        workflow_path = (
            project_dir / ".github" / "workflows" / "ci.yml"
        )
        if _write_file(
            workflow_path, workflow_content, force=force,
        ):
            files_written += 1

    if not _has_releaserc(project_dir):
        releaserc_content = _render_releaserc(project_name)
        releaserc_path = project_dir / ".releaserc.yaml"
        if _write_file(
            releaserc_path, releaserc_content, force=force,
        ):
            files_written += 1
    else:
        info("  Skipped .releaserc (already exists)")

    if files_written == 0:
        warn("No files written (all already exist)")
    else:
        success(f"Initialised {files_written} file(s)")

    return 0
