# Project:   HyperI CI
# File:      src/hyperi_ci/migrate.py
# Purpose:   Migrate projects from old ci/ submodule to hyperi-ci
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Migrate consumer projects from the old ci/ submodule approach.

The old CI system used a git submodule at ci/ pointing to hyperi-io/ci,
with workflows referencing ./ci/actions/ and ./ci/scripts/. This module
removes the submodule and replaces it with hyperi-ci generated files.

Migration steps:
  1. Detect old ci/ submodule or directory
  2. Remove submodule (deinit + rm) or plain directory
  3. Clean ci entry from .gitmodules (preserve other submodules)
  4. Remove old workflow files referencing ./ci/
  5. Run init to generate new workflow, Makefile, releaserc
  6. Preserve existing .hyperi-ci.yaml if present
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.detect import detect_language
from hyperi_ci.init import _build_prepare_cmd, detect_license, init_project

_OLD_CI_PATTERNS = (
    "./ci/actions",
    "./ci/scripts",
    "ci/actions/",
    "ci/scripts/",
    "uses: ./ci/",
)


def _is_git_repo(project_dir: Path) -> bool:
    """Check if the directory is inside a git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _has_ci_submodule(project_dir: Path) -> bool:
    """Check if ci/ is registered as a git submodule."""
    gitmodules = project_dir / ".gitmodules"
    if not gitmodules.exists():
        return False
    content = gitmodules.read_text()
    return "path = ci" in content or "path=ci" in content


def _has_ci_directory(project_dir: Path) -> bool:
    """Check if a ci/ directory exists (submodule or plain)."""
    return (project_dir / "ci").is_dir()


def _remove_ci_submodule(project_dir: Path) -> bool:
    """Remove the ci/ git submodule.

    Runs git submodule deinit and git rm, then cleans the
    .git/modules/ci cache directory.

    Args:
        project_dir: Project root directory.

    Returns:
        True if removal succeeded.
    """
    info("  Removing ci/ submodule...")

    deinit = subprocess.run(
        ["git", "submodule", "deinit", "-f", "ci"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if deinit.returncode != 0:
        warn(f"  submodule deinit warning: {deinit.stderr.strip()}")

    rm_result = subprocess.run(
        ["git", "rm", "-f", "ci"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if rm_result.returncode != 0:
        error(f"  Failed to remove ci/: {rm_result.stderr.strip()}")
        return False

    git_modules_cache = project_dir / ".git" / "modules" / "ci"
    if git_modules_cache.is_dir():
        shutil.rmtree(git_modules_cache)
        info("  Cleaned .git/modules/ci cache")

    success("  Removed ci/ submodule")
    return True


def _remove_ci_directory(project_dir: Path) -> bool:
    """Remove a plain ci/ directory (not a submodule).

    Args:
        project_dir: Project root directory.

    Returns:
        True if removal succeeded.
    """
    info("  Removing ci/ directory...")
    ci_dir = project_dir / "ci"
    shutil.rmtree(ci_dir)
    success("  Removed ci/ directory")
    return True


def _clean_gitmodules(project_dir: Path) -> None:
    """Remove the ci submodule entry from .gitmodules.

    If .gitmodules becomes empty after removal, remove the file.
    Otherwise stage the modified .gitmodules.
    """
    gitmodules = project_dir / ".gitmodules"
    if not gitmodules.exists():
        return

    content = gitmodules.read_text()

    # git rm of the submodule already removes the entry from .gitmodules
    # in most git versions — but if it's still there, clean it up
    if "path = ci" not in content and "path=ci" not in content:
        return

    # Remove the [submodule "ci"] block
    cleaned = re.sub(
        r'\[submodule\s+"ci"\]\s*\n(?:\s+\w[^\n]*\n)*',
        "",
        content,
    )
    cleaned = cleaned.strip()

    if not cleaned or "[submodule" not in cleaned:
        subprocess.run(
            ["git", "rm", "-f", ".gitmodules"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if gitmodules.exists():
            gitmodules.unlink()
        info("  Removed empty .gitmodules")
    else:
        gitmodules.write_text(cleaned + "\n")
        subprocess.run(
            ["git", "add", ".gitmodules"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        info("  Cleaned ci entry from .gitmodules (other submodules preserved)")


def _workflow_references_old_ci(path: Path) -> bool:
    """Check if a workflow file references the old ci/ submodule."""
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    return any(pattern in content for pattern in _OLD_CI_PATTERNS)


def _find_old_workflows(project_dir: Path) -> list[Path]:
    """Find workflow files that reference the old ci/ submodule."""
    workflows_dir = project_dir / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []

    results = []
    for pattern in ("*.yml", "*.yaml"):
        for f in workflows_dir.glob(pattern):
            if _workflow_references_old_ci(f):
                results.append(f)
    return results


def _remove_old_workflows(project_dir: Path) -> list[str]:
    """Remove workflow files referencing old ci/ and return their names.

    Args:
        project_dir: Project root directory.

    Returns:
        List of removed workflow filenames.
    """
    old_workflows = _find_old_workflows(project_dir)
    removed = []
    for wf in old_workflows:
        wf.unlink()
        removed.append(wf.name)
        info(f"  Removed old workflow: {wf.name}")
    return removed


def _find_old_ci_env_refs(project_dir: Path) -> list[str]:
    """Find files that might still reference old CI env vars or paths.

    Scans non-workflow files for old secret names and CI paths.
    Returns a list of warnings for the user to review manually.
    """
    warnings = []
    old_patterns = [
        re.compile(r"ARTIFACTORY_CI_USERNAME|ARTIFACTORY_CI_TOKEN"),
        re.compile(r"GH_APP_ID|GH_APP_PRIVATE_KEY"),
        re.compile(r"scripts-path:\s*\./ci/"),
        re.compile(r"\./ci/actions/"),
    ]

    workflows_dir = project_dir / ".github" / "workflows"
    search_globs = ["*.yml", "*.yaml", "Makefile", "Dockerfile"]

    ci_dir = project_dir / "ci"

    for glob_pattern in search_globs:
        for f in project_dir.rglob(glob_pattern):
            if ".git" in f.parts:
                continue
            # Skip ci/ directory — it's being removed entirely
            if ci_dir.is_dir():
                try:
                    f.relative_to(ci_dir)
                    continue
                except ValueError:
                    pass
            # Skip workflow dir — those get replaced separately
            if workflows_dir.is_dir():
                try:
                    f.relative_to(workflows_dir)
                    continue
                except ValueError:
                    pass
            try:
                content = f.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            for pattern in old_patterns:
                if pattern.search(content):
                    rel = f.relative_to(project_dir)
                    warnings.append(f"  {rel} may reference old CI secrets/paths")
                    break

    return warnings


_RELEASERC_CANDIDATES = (
    ".releaserc.json",
    ".releaserc.yaml",
    ".releaserc.yml",
    ".releaserc.js",
    "release.config.js",
    "release.config.cjs",
)

_OLD_CI_PREPARE_PATTERNS = (
    "./ci/scripts/",
    "ci/scripts/",
)

_LANGUAGE_GIT_ASSETS: dict[str, list[str]] = {
    "python": ["CHANGELOG.md", "VERSION", "pyproject.toml"],
    "rust": ["CHANGELOG.md", "VERSION", "Cargo.toml"],
    "typescript": ["CHANGELOG.md", "VERSION", "package.json"],
    "golang": ["CHANGELOG.md", "VERSION"],
}

_DEFAULT_GIT_ASSETS = ["CHANGELOG.md", "VERSION"]


def _find_releaserc(project_dir: Path) -> Path | None:
    """Find existing semantic-release config file."""
    for name in _RELEASERC_CANDIDATES:
        path = project_dir / name
        if path.exists():
            return path
    return None


def _fix_releaserc(
    project_dir: Path,
    language: str,
) -> bool:
    """Fix an existing .releaserc file for hyperi-ci migration.

    Replaces old ci/ script references in prepareCmd with a
    language-appropriate Python one-liner. Also cleans up git assets
    to only include language-relevant manifest files.

    Args:
        project_dir: Project root directory.
        language: Detected project language.

    Returns:
        True if the file was modified.
    """
    releaserc_path = _find_releaserc(project_dir)
    if releaserc_path is None:
        return False

    is_json = releaserc_path.suffix == ".json"
    if not is_json:
        info(f"  Skipped .releaserc fix (non-JSON format: {releaserc_path.name})")
        return False

    try:
        content = releaserc_path.read_text()
        config = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"  Could not parse {releaserc_path.name}: {exc}")
        return False

    modified = False

    plugins = config.get("plugins", [])
    for i, plugin in enumerate(plugins):
        if not isinstance(plugin, list) or len(plugin) < 2:
            continue

        plugin_name = plugin[0]
        plugin_config = plugin[1]

        if plugin_name == "@semantic-release/exec" and isinstance(plugin_config, dict):
            prepare_cmd = plugin_config.get("prepareCmd", "")
            if any(pat in prepare_cmd for pat in _OLD_CI_PREPARE_PATTERNS):
                new_cmd = _build_prepare_cmd(language)
                plugin_config["prepareCmd"] = new_cmd
                plugins[i] = [plugin_name, plugin_config]
                modified = True
                info("  Fixed prepareCmd (replaced old ci/ script reference)")

        if plugin_name == "@semantic-release/git" and isinstance(plugin_config, dict):
            old_assets = plugin_config.get("assets", [])
            expected_assets = _LANGUAGE_GIT_ASSETS.get(language, _DEFAULT_GIT_ASSETS)
            if set(old_assets) != set(expected_assets):
                plugin_config["assets"] = expected_assets
                plugins[i] = [plugin_name, plugin_config]
                modified = True
                removed = set(old_assets) - set(expected_assets)
                if removed:
                    info(
                        f"  Cleaned git assets (removed: {', '.join(sorted(removed))})"
                    )

    if modified:
        config["plugins"] = plugins
        output = json.dumps(config, indent=2) + "\n"
        releaserc_path.write_text(output)
        success(f"  Updated {releaserc_path.name}")

    return modified


def _clean_broken_ci_symlinks(project_dir: Path) -> int:
    """Remove broken symlinks that pointed to the old ci/ directory.

    After removing the ci/ submodule, any symlinks pointing into ci/
    become dangling. This finds and removes them.

    Args:
        project_dir: Project root directory.

    Returns:
        Number of broken symlinks removed.
    """
    count = 0
    for path in project_dir.rglob("*"):
        if path.is_symlink() and not path.exists():
            target = str(path.readlink())
            if target.startswith("ci/") or "/ci/" in target:
                rel = path.relative_to(project_dir)
                path.unlink()
                info(f"  Removed broken symlink: {rel} → {target}")
                count += 1
    return count


def _remove_old_non_ci_workflows(project_dir: Path) -> list[str]:
    """Remove workflow files that are superseded by the new CI system.

    The old CI used separate workflow files for different stages
    (publish.yml, semantic-release.yml). The new system consolidates
    into a single ci.yml that calls reusable workflows.

    Args:
        project_dir: Project root directory.

    Returns:
        List of removed workflow filenames.
    """
    workflows_dir = project_dir / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []

    superseded = [
        "publish.yml",
        "semantic-release.yml",
    ]

    removed = []
    for name in superseded:
        wf = workflows_dir / name
        if wf.exists():
            wf.unlink()
            removed.append(name)
            info(f"  Removed superseded workflow: {name}")

    return removed


def migrate_project(
    project_dir: Path,
    *,
    language: str | None = None,
    dry_run: bool = False,
) -> int:
    """Migrate a project from old ci/ submodule to hyperi-ci.

    Handles the full migration automatically:
      - Removes ci/ submodule or directory
      - Removes old workflow files (ci-referencing + superseded)
      - Fixes existing .releaserc (replaces ci/ script refs, cleans assets)
      - Generates new workflow, Makefile, config with correct license headers
      - Preserves existing .hyperi-ci.yaml if present

    Args:
        project_dir: Project root directory.
        language: Override detected language.
        dry_run: Show what would be done without making changes.

    Returns:
        Exit code (0 = success).
    """
    project_dir = project_dir.resolve()
    project_name = project_dir.name

    if not _is_git_repo(project_dir):
        error("Not a git repository")
        return 1

    has_submodule = _has_ci_submodule(project_dir)
    has_dir = _has_ci_directory(project_dir)
    old_workflows = _find_old_workflows(project_dir)
    releaserc_path = _find_releaserc(project_dir)

    if not has_submodule and not has_dir and not old_workflows:
        info("No old CI submodule or workflows found — nothing to migrate")
        info("Use 'hyperi-ci init' for new project setup")
        return 0

    detected = language or detect_language(project_dir)
    if not detected:
        error("Could not detect project language")
        info("Use --language: python, rust, typescript, golang")
        return 1

    has_existing_config = (project_dir / ".hyperi-ci.yaml").exists()
    license_id = detect_license(project_dir)

    info(f"Migrating {project_name} from old CI to hyperi-ci")
    info(f"  Language: {detected}, License: {license_id}")
    if has_submodule:
        info("  Found: ci/ submodule")
    elif has_dir:
        info("  Found: ci/ directory")
    if old_workflows:
        info(
            f"  Found: {len(old_workflows)} old workflow(s): "
            f"{', '.join(w.name for w in old_workflows)}"
        )
    if releaserc_path:
        info(f"  Found: {releaserc_path.name} (will fix ci/ references)")
    if has_existing_config:
        info("  Found: .hyperi-ci.yaml (will preserve)")

    ref_warnings = _find_old_ci_env_refs(project_dir)

    if dry_run:
        info("\n[DRY RUN] Would perform:")
        if has_submodule:
            info("  - Remove ci/ submodule")
        elif has_dir:
            info("  - Remove ci/ directory")
        if old_workflows:
            for wf in old_workflows:
                info(f"  - Remove old workflow: {wf.name}")
        info("  - Remove superseded workflows (publish.yml, semantic-release.yml)")
        if releaserc_path:
            info(f"  - Fix {releaserc_path.name} (prepareCmd + git assets)")
        info("  - Generate new .github/workflows/ci.yml")
        info("  - Generate Makefile (if no CI targets exist)")
        if not releaserc_path:
            info("  - Generate .releaserc.yaml")
        if not has_existing_config:
            info("  - Generate .hyperi-ci.yaml")
        if ref_warnings:
            warn("\nFiles that may need manual review:")
            for w in ref_warnings:
                warn(w)
        return 0

    if has_submodule:
        if not _remove_ci_submodule(project_dir):
            return 1
        _clean_gitmodules(project_dir)
    elif has_dir:
        if not _remove_ci_directory(project_dir):
            return 1

    _remove_old_workflows(project_dir)
    _remove_old_non_ci_workflows(project_dir)
    _clean_broken_ci_symlinks(project_dir)

    if releaserc_path:
        info("\nFixing semantic-release config...")
        _fix_releaserc(project_dir, detected)

    info("\nGenerating new CI files...")
    rc = init_project(
        project_dir,
        language=language,
        force=not has_existing_config,
    )

    if ref_warnings:
        warn("\nFiles that may need manual review:")
        for w in ref_warnings:
            warn(w)

    if rc == 0:
        success(f"\nMigration complete for {project_name}")
        info("Next steps:")
        info("  1. Review generated files (especially .github/workflows/ci.yml)")
        info("  2. git add -A && git commit -m 'fix: migrate to hyperi-ci'")
        info("  3. Push and verify CI runs")

    return rc
