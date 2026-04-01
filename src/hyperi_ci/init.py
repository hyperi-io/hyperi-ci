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
_DEFAULT_LICENSE = "FSL-1.1-ALv2"

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

_LICENSE_MARKERS = {
    "FSL-1.1-ALv2": ("FSL-1.1-ALv2",),
    "MIT": ("MIT License", "Permission is hereby granted"),
    "Apache-2.0": ("Apache License", "Licensed under the Apache"),
}


def detect_license(project_dir: Path) -> str:
    """Detect project license from LICENSE file or source file headers.

    Scans LICENSE file first, then falls back to checking source file
    headers for known license identifiers.

    Args:
        project_dir: Project root directory.

    Returns:
        License identifier string (e.g. "FSL-1.1-ALv2") or default.
    """
    license_file = project_dir / "LICENSE"
    if license_file.exists():
        try:
            content = license_file.read_text()[:2000]
            for license_id, markers in _LICENSE_MARKERS.items():
                if any(m in content for m in markers):
                    return license_id
        except (OSError, UnicodeDecodeError):
            pass

    scan_globs = ["*.py", "*.rs", "*.go", "*.ts", "*.sh", "Makefile"]
    for glob_pattern in scan_globs:
        for f in project_dir.glob(glob_pattern):
            try:
                header = f.read_text()[:500]
            except (OSError, UnicodeDecodeError):
                continue
            for license_id, markers in _LICENSE_MARKERS.items():
                if any(m in header for m in markers):
                    return license_id

    src_dir = project_dir / "src"
    if src_dir.is_dir():
        for f in src_dir.rglob("*.py"):
            try:
                header = f.read_text()[:500]
            except (OSError, UnicodeDecodeError):
                continue
            for license_id, markers in _LICENSE_MARKERS.items():
                if any(m in header for m in markers):
                    return license_id

    return _DEFAULT_LICENSE


def _license_header_text(license_id: str) -> str:
    """Return the license line for file headers."""
    if license_id == "FSL-1.1-ALv2":
        return "FSL-1.1-ALv2"
    return f"{license_id} — HYPERI PTY LIMITED"


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
    license_id: str = _DEFAULT_LICENSE,
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
        f"# License:   {_license_header_text(license_id)}\n"
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


def _render_makefile(project_name: str, license_id: str = _DEFAULT_LICENSE) -> str:
    """Render Makefile content with CI targets."""
    return (
        f"# Project:   {project_name}\n"
        "# File:      Makefile\n"
        "# Purpose:   CI targets wrapping hyperi-ci\n"
        "#\n"
        f"# License:   {_license_header_text(license_id)}\n"
        "# Copyright: (c) 2026 HYPERI PTY LIMITED\n"
        "\n"
        ".PHONY: quality test build check\n"
        "\n"
        "quality:\n"
        "\thyperi-ci run quality\n"
        "\n"
        "test:\n"
        "\thyperi-ci run test\n"
        "\n"
        "build:\n"
        "\thyperi-ci run build\n"
        "\n"
        "check:\n"
        "\thyperi-ci check\n"
    )


def _render_workflow(
    project_name: str,
    workflow_file: str,
    license_id: str = _DEFAULT_LICENSE,
    publish_target: str = "internal",
) -> str:
    """Render consumer .github/workflows/ci.yml content."""
    base = (
        f"# Project:   {project_name}\n"
        "# File:      .github/workflows/ci.yml\n"
        "# Purpose:   CI pipeline\n"
        "#\n"
        f"# License:   {_license_header_text(license_id)}\n"
        "# Copyright: (c) 2026 HYPERI PTY LIMITED\n"
        "\n"
        "name: CI\n"
        "\n"
        '"on":\n'
        "  push:\n"
        '    branches: ["**"]\n'
        "  pull_request:\n"
        "    branches: [main]\n"
        "  workflow_dispatch:\n"
        "\n"
        "jobs:\n"
        "  ci:\n"
        f"    uses: {_CI_REPO}/.github/workflows/"
        f"{workflow_file}@{_WORKFLOW_REF}\n"
    )

    if publish_target != "internal":
        base += f"    with:\n      publish-target: {publish_target}\n"

    base += "    secrets: inherit\n"
    return base


def _build_prepare_cmd(language: str) -> str:
    """Build the prepareCmd for semantic-release @semantic-release/exec.

    Generates a Python one-liner that writes the VERSION file and
    updates the language-specific manifest (pyproject.toml, Cargo.toml, etc.).

    Args:
        language: Detected project language.

    Returns:
        Shell command string for prepareCmd.
    """
    base = "from pathlib import Path; "
    version_write = "Path('VERSION').write_text('${nextRelease.version}\\n')"

    if language == "python":
        return (
            'python3 -c "'
            f"{base}import re; "
            f"{version_write}; "
            "p=Path('pyproject.toml'); t=p.read_text(); "
            't=re.sub(r\'^version\\\\s*=\\\\s*\\"[^\\"]*\\"\', '
            "'version = \\\"${nextRelease.version}\\\"', "
            "t, count=1, flags=re.MULTILINE); "
            'p.write_text(t)"'
        )

    if language == "rust":
        return (
            'python3 -c "'
            f"{base}import re; "
            f"{version_write}; "
            "ct=Path('Cargo.toml').read_text(); "
            'ct=re.sub(r\'^version\\\\s*=\\\\s*\\"[^\\"]*\\"\', '
            "'version = \\\"${nextRelease.version}\\\"', "
            "ct, count=1, flags=re.MULTILINE); "
            "Path('Cargo.toml').write_text(ct)\""
        )

    if language == "typescript":
        return (
            'python3 -c "'
            f"{base}import json; "
            f"{version_write}; "
            "p=Path('package.json'); d=json.loads(p.read_text()); "
            "d['version']='${nextRelease.version}'; "
            "p.write_text(json.dumps(d, indent=2)+'\\n')\""
        )

    if language == "golang":
        return f'python3 -c "{base}{version_write}"'

    return f'python3 -c "{base}{version_write}"'


def _render_releaserc(
    project_name: str,
    language: str = "",
    license_id: str = _DEFAULT_LICENSE,
) -> str:
    """Render .releaserc.yaml for semantic-release.

    Args:
        project_name: Project name for header.
        language: Detected language (affects prepareCmd and git assets).
        license_id: License identifier for file header.
    """
    prepare_cmd = _build_prepare_cmd(language)

    git_assets = ["CHANGELOG.md", "VERSION"]
    if language == "rust":
        git_assets.append("Cargo.toml")
    elif language == "python":
        git_assets.append("pyproject.toml")
    elif language == "typescript":
        git_assets.append("package.json")

    config: dict = {
        "branches": ["main"],
        "tagFormat": "v${version}",
        "plugins": [
            [
                "@semantic-release/commit-analyzer",
                {
                    "preset": "conventionalcommits",
                    "releaseRules": [
                        {"breaking": True, "release": "major"},
                        {"type": "feat", "release": "minor"},
                        {"type": "fix", "release": "patch"},
                        {"type": "perf", "release": "patch"},
                        {"type": "sec", "release": "patch"},
                        {"type": "hotfix", "release": "patch"},
                        {"type": "security", "release": "patch"},
                        {"type": "docs", "release": False},
                        {"type": "test", "release": False},
                        {"type": "refactor", "release": False},
                        {"type": "style", "release": False},
                        {"type": "build", "release": False},
                        {"type": "ci", "release": False},
                        {"type": "chore", "release": False},
                        {"type": "deps", "release": False},
                        {"type": "revert", "release": False},
                        {"type": "wip", "release": False},
                        {"type": "cleanup", "release": False},
                        {"type": "data", "release": False},
                        {"type": "debt", "release": False},
                        {"type": "design", "release": False},
                        {"type": "infra", "release": False},
                        {"type": "meta", "release": False},
                        {"type": "ops", "release": False},
                        {"type": "review", "release": False},
                        {"type": "spike", "release": False},
                        {"type": "ui", "release": False},
                    ],
                },
            ],
            "@semantic-release/release-notes-generator",
            [
                "@semantic-release/changelog",
                {"changelogFile": "CHANGELOG.md"},
            ],
            [
                "@semantic-release/exec",
                {"prepareCmd": prepare_cmd},
            ],
            [
                "@semantic-release/git",
                {
                    "assets": git_assets,
                    "message": "chore: version ${nextRelease.version} [skip ci]",
                },
            ],
        ],
    }

    header = (
        f"# Project:   {project_name}\n"
        "# File:      .releaserc.yaml\n"
        "# Purpose:   Semantic release configuration\n"
        "#\n"
        f"# License:   {_license_header_text(license_id)}\n"
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
        warn(f"  Skipped {path.name} (exists, use --force to overwrite)")
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

    license_id = detect_license(project_dir)
    info(f"Initialising {project_name} as {detected} project (license: {license_id})")

    workflow_file = _LANGUAGE_WORKFLOW_MAP.get(detected)
    if not workflow_file:
        warn(f"No reusable workflow for {detected}")
        warn("Generating config only")

    files_written = 0

    config_content = _render_hyperi_ci_yaml(
        detected,
        project_name,
        project_dir,
        license_id=license_id,
    )
    config_path = project_dir / ".hyperi-ci.yaml"
    if _write_file(config_path, config_content, force=force):
        files_written += 1

    if _makefile_has_ci_targets(project_dir) and not force:
        info("  Skipped Makefile (already has CI targets)")
    else:
        makefile_content = _render_makefile(project_name, license_id=license_id)
        makefile_path = project_dir / "Makefile"
        if _write_file(
            makefile_path,
            makefile_content,
            force=force,
        ):
            files_written += 1

    if workflow_file:
        workflow_content = _render_workflow(
            project_name,
            workflow_file,
            license_id=license_id,
        )
        workflow_path = project_dir / ".github" / "workflows" / "ci.yml"
        if _write_file(
            workflow_path,
            workflow_content,
            force=force,
        ):
            files_written += 1

    if not _has_releaserc(project_dir):
        releaserc_content = _render_releaserc(
            project_name,
            language=detected,
            license_id=license_id,
        )
        releaserc_path = project_dir / ".releaserc.yaml"
        if _write_file(
            releaserc_path,
            releaserc_content,
            force=force,
        ):
            files_written += 1
    else:
        info("  Skipped .releaserc (already exists)")

    hook_path = project_dir / ".githooks" / "commit-msg"
    if not hook_path.exists() or force:
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(
            "#!/usr/bin/env bash\n"
            "# Conventional commit validation hook\n"
            "# Install: git config core.hooksPath .githooks\n"
            "\n"
            "if command -v hyperi-ci >/dev/null 2>&1; then\n"
            '    hyperi-ci check-commit "$1"\n'
            "elif command -v uvx >/dev/null 2>&1; then\n"
            '    uvx hyperi-ci check-commit "$1"\n'
            "else\n"
            '    echo "Warning: hyperi-ci not found — skipping commit validation" >&2\n'
            "    exit 0\n"
            "fi\n"
        )
        hook_path.chmod(0o755)
        info(f"  Created: {hook_path}")
        files_written += 1
    else:
        info("  Skipped .githooks/commit-msg (already exists)")

    if files_written == 0:
        warn("No files written (all already exist)")
    else:
        success(f"Initialised {files_written} file(s)")

    return 0
