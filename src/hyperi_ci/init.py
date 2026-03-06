# Project:   HyperI CI
# File:      src/hyperi_ci/init.py
# Purpose:   Project scaffolding — generates config, Makefile, and workflow
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Project initialisation for hyperi-ci.

Generates the three files consumer projects need:
  - .hyperi-ci.yaml (CI configuration)
  - Makefile (quality/test/build targets)
  - .github/workflows/ci.yml (reusable workflow caller)
"""

from __future__ import annotations

from pathlib import Path

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


def _render_hyperi_ci_yaml(language: str, project_name: str) -> str:
    """Render .hyperi-ci.yaml content for a project."""
    return f"""\
# Project:   {project_name}
# File:      .hyperi-ci.yaml
# Purpose:   HyperI CI configuration
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

language: {language}

quality:
  enabled: true

test:
  enabled: true

build:
  enabled: true
  strategies:
    - native

publish:
  enabled: true
  target: internal
"""


def _render_makefile(project_name: str) -> str:
    """Render Makefile content with CI targets."""
    return f"""\
# Project:   {project_name}
# File:      Makefile
# Purpose:   CI targets wrapping hyperi-ci
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

.PHONY: quality test build

quality:
\thyperi-ci run quality

test:
\thyperi-ci run test

build:
\thyperi-ci run build
"""


def _render_workflow(
    language: str,
    project_name: str,
    workflow_file: str,
) -> str:
    """Render consumer .github/workflows/ci.yml content."""
    return f"""\
# Project:   {project_name}
# File:      .github/workflows/ci.yml
# Purpose:   CI pipeline
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: {_CI_REPO}/.github/workflows/{workflow_file}@{_WORKFLOW_REF}
    secrets: inherit
"""


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
        warn(f"  Skipped {path} (already exists, use --force to overwrite)")
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    success(f"  Created {path}")
    return True


def init_project(
    project_dir: Path,
    *,
    language: str | None = None,
    force: bool = False,
) -> int:
    """Initialise a consumer project for hyperi-ci.

    Generates .hyperi-ci.yaml, Makefile, and .github/workflows/ci.yml
    in the target directory.

    Args:
        project_dir: Project root directory.
        language: Override detected language.
        force: Overwrite existing files.

    Returns:
        Exit code (0 = success).
    """
    project_dir = project_dir.resolve()
    project_name = project_dir.name

    detected = language or detect_language(project_dir)
    if not detected:
        error("Could not detect project language")
        info("Use --language to specify: python, rust, typescript, golang")
        return 1

    info(f"Initialising {project_name} as {detected} project")

    workflow_file = _LANGUAGE_WORKFLOW_MAP.get(detected)
    if not workflow_file:
        warn(f"No reusable workflow template for {detected}")
        warn("Generating config and Makefile only")

    files_written = 0

    config_content = _render_hyperi_ci_yaml(detected, project_name)
    if _write_file(project_dir / ".hyperi-ci.yaml", config_content, force=force):
        files_written += 1

    makefile_content = _render_makefile(project_name)
    if _write_file(project_dir / "Makefile", makefile_content, force=force):
        files_written += 1

    if workflow_file:
        workflow_content = _render_workflow(detected, project_name, workflow_file)
        workflow_path = project_dir / ".github" / "workflows" / "ci.yml"
        if _write_file(workflow_path, workflow_content, force=force):
            files_written += 1

    if files_written == 0:
        warn("No files written (all already exist)")
    else:
        success(f"Initialised {files_written} file(s)")

    return 0
