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

import re
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.init import init_project

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

    for glob_pattern in search_globs:
        for f in project_dir.rglob(glob_pattern):
            if ".git" in f.parts:
                continue
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


def migrate_project(
    project_dir: Path,
    *,
    language: str | None = None,
    dry_run: bool = False,
) -> int:
    """Migrate a project from old ci/ submodule to hyperi-ci.

    Preserves the existing .hyperi-ci.yaml if present.
    Always overwrites workflow files and Makefile since those
    need to change for the new system.

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

    if not has_submodule and not has_dir and not old_workflows:
        info("No old CI submodule or workflows found — nothing to migrate")
        info("Use 'hyperi-ci init' for new project setup")
        return 0

    has_existing_config = (project_dir / ".hyperi-ci.yaml").exists()

    info(f"Migrating {project_name} from old CI to hyperi-ci")
    if has_submodule:
        info("  Found: ci/ submodule")
    elif has_dir:
        info("  Found: ci/ directory")
    if old_workflows:
        info(
            f"  Found: {len(old_workflows)} old workflow(s): "
            f"{', '.join(w.name for w in old_workflows)}"
        )
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
        info("  - Generate new .github/workflows/ci.yml")
        info("  - Generate Makefile (if no CI targets exist)")
        info("  - Generate .releaserc.yaml (if not present)")
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

    # Run init — force workflow generation but preserve existing config
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
