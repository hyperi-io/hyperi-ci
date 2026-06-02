# Project:   HyperI CI
# File:      src/hyperi_ci/init.py
# Purpose:   Project scaffolding — generates config, Makefile, and workflow
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Project initialisation for hyperi-ci.

Generates the files consumer projects need for CI integration.
Existing-project-aware: detects deprecated configs, existing Makefiles
with CI targets, and generates language-appropriate defaults.

Generated files:
  - .hyperi-ci.yaml (CI configuration, language-specific defaults)
  - Makefile (quality/test/build targets — skipped if existing)
  - .github/workflows/ci.yml (reusable workflow caller)

No .releaserc is generated: hyperi-ci uses a central tagger-only
semantic-release config (provided at CI time) and `hyperi-ci stamp-version`
for stamping. Scaffolding a per-repo .releaserc with @semantic-release/git
is what caused the issue #37 tag-rewrite damage.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.detect import detect_language

_CI_REPO = "hyperi-io/hyperi-ci"
_WORKFLOW_REF = "main"
_DEFAULT_LICENSE = "BUSL-1.1"

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
    "BUSL-1.1": ("BUSL-1.1", "Business Source License"),
    "MIT": ("MIT License", "Permission is hereby granted"),
    "Apache-2.0": ("Apache License", "Licensed under the Apache"),
}


def detect_license(project_dir: Path) -> str:
    """Resolve the project licence: explicit declaration, else scan, else default.

    Order: an explicit ``license:`` in ``.hyperi-ci.yaml`` wins (the project
    declares its licence), then the LICENSE file, then source-file headers, then
    the default (BUSL-1.1).

    Args:
        project_dir: Project root directory.

    Returns:
        License identifier string (e.g. "BUSL-1.1").

    """
    ci_config = project_dir / ".hyperi-ci.yaml"
    if ci_config.exists():
        try:
            data = yaml.safe_load(ci_config.read_text()) or {}
            declared = data.get("license") if isinstance(data, dict) else None
            if isinstance(declared, str) and declared.strip():
                return declared.strip()
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            pass

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
    if license_id == "BUSL-1.1":
        return "BUSL-1.1"
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
        # Declared SPDX-ish licence id. Drives generated file headers and is
        # the authoritative source for `detect_license`. Default BUSL-1.1.
        "license": license_id,
        # Information-only: lifecycle stage of the project. Surfaced
        # in CI logs and `hyperi-ci config`. Does not gate any
        # behaviour. New projects default to `experimental` — bump as
        # the project matures. Values: experimental | alpha | beta |
        # ga | legacy | deprecated.
        "project": {"status": "experimental"},
        "quality": {"enabled": True},
        "test": {"enabled": True},
        "build": {"enabled": True, "strategies": ["native"]},
        # JFrog removed in v2.1.4 — all publishing is OSS. `target` is a
        # legacy no-op kept for back-compat; oss is the only meaningful value.
        "publish": {"enabled": True, "target": "oss"},
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
        "    inputs:\n"
        "      tag:\n"
        "        type: string\n"
        "        required: false\n"
        '        default: ""\n'
        '        description: "Tag to re-publish (existing tag). Omit + set from-head to release HEAD."\n'
        "      from-head:\n"
        "        type: string\n"
        "        required: false\n"
        '        default: ""\n'
        "        description: \"'true' to release/retry the current HEAD — the CI creates the tag (issue #35).\"\n"
        "      bump:\n"
        "        type: string\n"
        "        required: false\n"
        '        default: "auto"\n'
        '        description: "Version resolution for from-head: auto | patch | minor (forced — release even with no release-worthy commit)."\n'
        "\n"
        "jobs:\n"
        "  ci:\n"
        f"    uses: {_CI_REPO}/.github/workflows/"
        f"{workflow_file}@{_WORKFLOW_REF}\n"
        "    with:\n"
        "      tag: ${{ inputs.tag || '' }}\n"
        "      from-head: ${{ inputs.from-head || '' }}\n"
        "      bump: ${{ inputs.bump || 'auto' }}\n"
    )

    if publish_target != "internal":
        base += f"      publish-target: {publish_target}\n"

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


def _render_contributing(project_name: str) -> str:
    """Render CONTRIBUTING.md template.

    Separates maintainer setup (strict tooling, hooks, hyperi-ci CLI)
    from external-contributor experience (standard tooling, no
    setup-by-default).
    """
    return (
        f"# Contributing to {project_name}\n"
        "\n"
        "Two audiences, two paths.\n"
        "\n"
        "## External contributors -- the short path\n"
        "\n"
        "**You do NOT need to install `hyperi-ci` or activate any\n"
        "repo-local git hooks.** Standard tooling is enough.\n"
        "\n"
        "1. Fork the repository, clone your fork, create a topic branch.\n"
        "2. Make your change. Run the project's tests with whatever the\n"
        "   language ecosystem provides (`pytest`, `cargo test`,\n"
        "   `npm test`, `go test ./...`). Lint via the standard tools\n"
        "   the project declares in its manifest (`ruff`, `clippy`,\n"
        "   `eslint`, `golangci-lint`, etc.). The project's pyproject /\n"
        "   Cargo.toml / package.json carries the lint config; your IDE\n"
        "   picks it up without further setup.\n"
        "3. Commit with whatever message format your workflow uses.\n"
        "   Open a PR against `main`.\n"
        "\n"
        "**What happens to your PR's CI checks:**\n"
        "\n"
        "PRs from forks intentionally do not auto-trigger this repo's\n"
        "full CI pipeline. You will see green checks because all jobs\n"
        "are skipped, not because they ran and passed. A maintainer\n"
        "will pull your PR locally to validate it against the full\n"
        "pipeline. This avoids exposing internal CI credentials and\n"
        "self-hosted runners to fork-originated workflows, which is the\n"
        "standard GitHub-side security recommendation.\n"
        "\n"
        "If a maintainer requests changes, push to the same branch on\n"
        "your fork. They will re-validate.\n"
        "\n"
        "## Maintainers -- the strict path\n"
        "\n"
        "Maintainers opt in to the project's stricter tooling:\n"
        "\n"
        "```bash\n"
        "# install the CLI\n"
        "uv tool install hyperi-ci          # or: pipx install hyperi-ci\n"
        "\n"
        "# activate the repo-local git hooks (commit-msg validation +\n"
        "# pre-push enforcement of `hyperi-ci push` over bare `git push`)\n"
        "git config core.hooksPath .githooks\n"
        "\n"
        "# verify\n"
        "hyperi-ci --version\n"
        "hyperi-ci check                    # quality + test, pre-push gate\n"
        "```\n"
        "\n"
        "Maintainer workflow:\n"
        "\n"
        "1. Land changes on `main` via PR or direct push (your call).\n"
        "2. `hyperi-ci push` instead of `git push`. The pre-push hook\n"
        "   enforces this; bypass with `HYPERCI_PUSH=1 git push` if you\n"
        "   know what you are doing.\n"
        "3. `hyperi-ci push --publish` when you want to ship a release.\n"
        "   Amends a `Publish: true` trailer to HEAD; the CI pipeline\n"
        "   picks that up, predicts the next version, stamps it, and\n"
        "   publishes.\n"
        "\n"
        "## What happens when you push commits to your fork\n"
        "\n"
        "Nothing on this repo's side. Your fork has its own GitHub\n"
        "Actions context; this repo's workflows are not triggered until\n"
        "you open a PR. Your fork's own CI (if you enabled it) runs in\n"
        "your namespace.\n"
        "\n"
        "## Commit message conventions\n"
        "\n"
        "Maintainers follow Conventional Commits and the hooks enforce\n"
        "it. External contributors do not need to follow this format --\n"
        "the maintainer who merges your PR rewrites the merge commit\n"
        "as needed.\n"
        "\n"
        "## Security disclosures\n"
        "\n"
        "Do NOT open a public issue or PR for security vulnerabilities.\n"
        "See the repository's `SECURITY.md` (if present) or the\n"
        "organisation's security contact for the disclosure process.\n"
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

    # No .releaserc is scaffolded (issue #37). hyperi-ci uses a central
    # tagger-only semantic-release config provided by the setup-semantic-release
    # composite at CI time; version stamping is `hyperi-ci stamp-version`. A
    # scaffolded .releaserc with @semantic-release/git is exactly what rewrote
    # tags off-main and destroyed release history. A genuine exception goes in
    # `.hyperi-ci.yaml`, not a raw per-repo file.
    if _has_releaserc(project_dir):
        info(
            "  Left existing .releaserc in place (honoured if it has no "
            "@semantic-release/git|github plugin; else ignored at CI time)"
        )

    # CONTRIBUTING.md -- only generated if absent. Repos that already
    # have their own contributing guide keep it; we do not overwrite.
    contributing_path = project_dir / "CONTRIBUTING.md"
    if not contributing_path.exists() or force:
        contributing_content = _render_contributing(project_name)
        if _write_file(
            contributing_path,
            contributing_content,
            force=force,
        ):
            files_written += 1
    else:
        info("  Skipped CONTRIBUTING.md (already exists)")

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

    pre_push_path = project_dir / ".githooks" / "pre-push"
    if not pre_push_path.exists() or force:
        pre_push_path.parent.mkdir(parents=True, exist_ok=True)
        pre_push_path.write_text(
            "#!/usr/bin/env bash\n"
            "# Enforce hyperi-ci push instead of bare git push\n"
            "# hyperi-ci push sets HYPERCI_PUSH=1 before calling git push\n"
            "\n"
            'if [ "${HYPERCI_PUSH:-}" = "1" ]; then\n'
            "    exit 0\n"
            "fi\n"
            "\n"
            "echo ''\n"
            "echo '  Push blocked. Use hyperi-ci push instead of git push.'\n"
            "echo ''\n"
            "echo '    hyperi-ci push            # normal push with pre-checks'\n"
            "echo '    hyperi-ci push --release  # push + auto-publish if CI passes'\n"
            "echo '    hyperi-ci push --no-ci    # push, skip CI'\n"
            "echo ''\n"
            "echo '  To bypass (emergency): HYPERCI_PUSH=1 git push'\n"
            "echo ''\n"
            "exit 1\n"
        )
        pre_push_path.chmod(0o755)
        info(f"  Created: {pre_push_path}")
        files_written += 1
    else:
        info("  Skipped .githooks/pre-push (already exists)")

    if files_written == 0:
        warn("No files written (all already exist)")
    else:
        success(f"Initialised {files_written} file(s)")

    return 0
