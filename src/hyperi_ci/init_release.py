# Project:   HyperI CI
# File:      src/hyperi_ci/init_release.py
# Purpose:   Set up release branch and two-channel semantic-release
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Set up a release branch and configure two-channel semantic-release.

After running init-release:
- `main` branch → dev pre-releases (v1.15.0-dev.1)
- `release` branch → GA releases (v1.15.0)

Usage:
    hyperi-ci init-release              # set up release branch + update .releaserc
    hyperi-ci init-release --check      # check if release setup is correct
    hyperi-ci init-release --dry-run    # show what would be done
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from hyperi_ci.common import error, info, success, warn


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kwargs)


def _has_release_branch(remote: bool = True) -> bool:
    """Check if release branch exists locally or remotely."""
    local = _run(["git", "rev-parse", "--verify", "release"])
    if local.returncode == 0:
        return True
    if remote:
        remote_check = _run(["git", "ls-remote", "--heads", "origin", "release"])
        return "refs/heads/release" in (remote_check.stdout or "")
    return False


def _get_releaserc_path(project_dir: Path) -> Path | None:
    """Find the .releaserc file (YAML or JSON)."""
    for name in (".releaserc.yaml", ".releaserc.yml", ".releaserc.json", ".releaserc"):
        path = project_dir / name
        if path.exists():
            return path
    return None


def _load_releaserc(path: Path) -> tuple[dict, str]:
    """Load releaserc config. Returns (config_dict, format)."""
    content = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(content), "yaml"
    return json.loads(content), "json"


def _has_two_branch_config(config: dict) -> bool:
    """Check if the releaserc already has two-branch (main+release) config."""
    branches = config.get("branches", [])
    if not isinstance(branches, list):
        return False

    branch_names = set()
    for b in branches:
        if isinstance(b, str):
            branch_names.add(b)
        elif isinstance(b, dict) and "name" in b:
            branch_names.add(b["name"])

    return "main" in branch_names and "release" in branch_names


def _update_releaserc(path: Path, *, dry_run: bool = False) -> bool:
    """Update .releaserc to two-branch config. Returns True if changed."""
    config, fmt = _load_releaserc(path)

    if _has_two_branch_config(config):
        info("  .releaserc already has two-branch config")
        return False

    config["branches"] = [
        {"name": "main", "prerelease": "dev"},
        "release",
    ]

    if dry_run:
        info(f"  Would update {path.name} with two-branch config")
        return True

    content = path.read_text()

    if fmt == "yaml":
        # Targeted replacement: only change the branches block, preserve everything else
        new_branches = "branches:\n  - name: main\n    prerelease: dev\n  - release\n"
        # Match various forms: "branches:\n  - main\n" or "branches:\n- main\n"
        import re

        pattern = re.compile(
            r"^branches:\s*\n(?:\s+-\s+.*\n)*",
            re.MULTILINE,
        )
        new_content = pattern.sub(new_branches, content, count=1)
        if new_content == content:
            # Fallback: couldn't find branches block to replace
            warn(
                f"  Could not find branches block in {path.name} — manual update needed"
            )
            return False
        path.write_text(new_content)
    else:
        path.write_text(json.dumps(config, indent=2) + "\n")

    info(f"  Updated {path.name} with two-branch config")
    return True


def _update_consumer_workflow(project_dir: Path, *, dry_run: bool = False) -> bool:
    """Ensure consumer workflow triggers on release branch pushes too.

    Returns True if changed.
    """
    wf_dir = project_dir / ".github" / "workflows"
    if not wf_dir.exists():
        return False

    changed = False
    for wf_file in sorted(wf_dir.glob("*.yml")):
        content = wf_file.read_text()
        # Skip if it already triggers on release branch or all branches
        if 'branches: ["**"]' in content or "release" in content:
            continue
        # Only modify files that have push triggers on main
        if "branches: [main]" in content and "push:" in content:
            if dry_run:
                info(f"  Would update {wf_file.name} to trigger on release branch")
                changed = True
                continue
            new_content = content.replace(
                "branches: [main]",
                "branches: [main, release]",
                1,  # Only replace the first occurrence (push trigger, not PR trigger)
            )
            if new_content != content:
                wf_file.write_text(new_content)
                info(f"  Updated {wf_file.name} to trigger on release branch")
                changed = True

    return changed


def check_release_setup(project_dir: Path) -> bool:
    """Check if release branch and config are properly set up."""
    ok = True

    if not _has_release_branch():
        warn("  Release branch does not exist (local or remote)")
        ok = False
    else:
        info("  Release branch exists")

    releaserc = _get_releaserc_path(project_dir)
    if not releaserc:
        warn("  No .releaserc file found")
        ok = False
    else:
        config, _ = _load_releaserc(releaserc)
        if _has_two_branch_config(config):
            info("  .releaserc has two-branch config")
        else:
            warn("  .releaserc missing two-branch config (main + release)")
            ok = False

    return ok


def init_release(
    project_dir: Path,
    *,
    dry_run: bool = False,
    check_only: bool = False,
) -> int:
    """Set up release branch and configure semantic-release channels.

    Returns 0 on success, 1 on failure.
    """
    info(f"Release setup for {project_dir.name}")

    if check_only:
        ok = check_release_setup(project_dir)
        return 0 if ok else 1

    # Validate: must be a git repo
    git_check = _run(["git", "rev-parse", "--git-dir"], cwd=project_dir)
    if git_check.returncode != 0:
        error("Not a git repository")
        return 1

    # Check for .hyperi-ci.yaml
    if not (project_dir / ".hyperi-ci.yaml").exists():
        warn("No .hyperi-ci.yaml found — run 'hyperi-ci init' first")

    changes_made = False

    # Step 1: Update .releaserc
    releaserc = _get_releaserc_path(project_dir)
    if releaserc:
        if _update_releaserc(releaserc, dry_run=dry_run):
            changes_made = True
    else:
        warn("  No .releaserc file found — run 'hyperi-ci init' to create one")

    # Step 2: Update consumer workflow to trigger on release branch
    if _update_consumer_workflow(project_dir, dry_run=dry_run):
        changes_made = True

    # Step 3: Create release branch if it doesn't exist
    if not _has_release_branch():
        if dry_run:
            info("  Would create release branch from main")
            changes_made = True
        else:
            # Create from current main HEAD
            result = _run(["git", "branch", "release", "main"], cwd=project_dir)
            if result.returncode != 0:
                error(f"Failed to create release branch: {result.stderr.strip()}")
                return 1
            info("  Created release branch from main")

            # Push the release branch
            result = _run(
                ["git", "push", "-u", "origin", "release"],
                cwd=project_dir,
            )
            if result.returncode != 0:
                error(f"Failed to push release branch: {result.stderr.strip()}")
                return 1
            info("  Pushed release branch to origin")
            changes_made = True
    else:
        info("  Release branch already exists")

    if dry_run:
        if changes_made:
            info("\nDry run — no changes made. Run without --dry-run to apply.")
        else:
            info("\nRelease setup already complete — nothing to do.")
        return 0

    if changes_made:
        success("Release setup complete. Commit and push the config changes.")
    else:
        info("Release setup already complete — nothing to do.")

    return 0
