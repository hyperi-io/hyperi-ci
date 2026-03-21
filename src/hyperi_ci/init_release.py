# Project:   HyperI CI
# File:      src/hyperi_ci/init_release.py
# Purpose:   Set up release branches and multi-channel semantic-release
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Set up release branches and configure multi-channel semantic-release.

Available channels (ordered by stability):
- ``main``    -> dev pre-releases  (v1.15.0-dev.1)
- ``alpha``   -> alpha releases    (v1.15.0-alpha.1)
- ``beta``    -> beta releases     (v1.15.0-beta.1)
- ``release`` -> GA releases       (v1.15.0)

Usage:
    hyperi-ci init-release                          # default: main + release
    hyperi-ci init-release --channels alpha,beta    # add alpha + beta channels
    hyperi-ci init-release --check                  # check setup status
    hyperi-ci init-release --dry-run                # show what would be done
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from hyperi_ci.common import error, info, success, warn

# Ordered channel definitions — order matters for semantic-release.
# Each entry: (branch_name, prerelease_tag_or_None)
ALL_CHANNELS: list[tuple[str, str | None]] = [
    ("main", "dev"),
    ("alpha", "alpha"),
    ("beta", "beta"),
    ("release", None),
]

# The minimum viable config: dev pre-releases on main, GA on release.
DEFAULT_CHANNELS = {"main", "release"}


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kwargs)


def _has_branch(name: str, remote: bool = True) -> bool:
    """Check if a branch exists locally or remotely."""
    local = _run(["git", "rev-parse", "--verify", name])
    if local.returncode == 0:
        return True
    if remote:
        remote_check = _run(["git", "ls-remote", "--heads", "origin", name])
        return f"refs/heads/{name}" in (remote_check.stdout or "")
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


def _configured_channels(config: dict) -> set[str]:
    """Extract the set of branch names from a releaserc config."""
    branches = config.get("branches", [])
    if not isinstance(branches, list):
        return set()
    names: set[str] = set()
    for b in branches:
        if isinstance(b, str):
            names.add(b)
        elif isinstance(b, dict) and "name" in b:
            names.add(b["name"])
    return names


def _has_required_channels(config: dict, channels: set[str]) -> bool:
    """Check if the releaserc has all required channels configured."""
    return channels.issubset(_configured_channels(config))


def _build_branches_config(channels: set[str]) -> list[dict | str]:
    """Build the semantic-release branches array in correct order."""
    branches: list[dict | str] = []
    for name, prerelease in ALL_CHANNELS:
        if name not in channels:
            continue
        if prerelease:
            branches.append({"name": name, "prerelease": prerelease})
        else:
            branches.append(name)
    return branches


def _build_branches_yaml(channels: set[str]) -> str:
    """Build the YAML string for the branches block."""
    lines = ["branches:\n"]
    for name, prerelease in ALL_CHANNELS:
        if name not in channels:
            continue
        if prerelease:
            lines.append(f"  - name: {name}\n")
            lines.append(f"    prerelease: {prerelease}\n")
        else:
            lines.append(f"  - {name}\n")
    return "".join(lines)


def _update_releaserc(
    path: Path,
    channels: set[str],
    *,
    dry_run: bool = False,
) -> bool:
    """Update .releaserc with the requested channels. Returns True if changed."""
    config, fmt = _load_releaserc(path)

    if _has_required_channels(config, channels):
        info(f"  .releaserc already has channels: {', '.join(sorted(channels))}")
        return False

    config["branches"] = _build_branches_config(channels)

    if dry_run:
        info(f"  Would update {path.name} with channels: {', '.join(sorted(channels))}")
        return True

    if fmt == "yaml":
        content = path.read_text()
        new_branches = _build_branches_yaml(channels)
        pattern = re.compile(
            r"^branches:\s*\n(?:\s+-\s+.*\n)*",
            re.MULTILINE,
        )
        new_content = pattern.sub(new_branches, content, count=1)
        if new_content == content:
            warn(
                f"  Could not find branches block in {path.name} — manual update needed"
            )
            return False
        path.write_text(new_content)
    else:
        path.write_text(json.dumps(config, indent=2) + "\n")

    info(f"  Updated {path.name} with channels: {', '.join(sorted(channels))}")
    return True


def _update_consumer_workflow(
    project_dir: Path,
    channels: set[str],
    *,
    dry_run: bool = False,
) -> bool:
    """Ensure consumer workflow triggers on all channel branches.

    Returns True if changed.
    """
    wf_dir = project_dir / ".github" / "workflows"
    if not wf_dir.exists():
        return False

    # Build the branches list for the workflow trigger
    branch_names = sorted(channels)
    branches_str = ", ".join(branch_names)

    changed = False
    for wf_file in sorted(wf_dir.glob("*.yml")):
        content = wf_file.read_text()
        if 'branches: ["**"]' in content:
            continue
        # Match existing branches: [main] or branches: [main, release] etc.
        pattern = re.compile(r"branches:\s*\[([^\]]+)\]")
        match = pattern.search(content)
        if not match or "push:" not in content:
            continue
        existing = match.group(1)
        existing_set = {b.strip() for b in existing.split(",")}
        if channels.issubset(existing_set):
            continue
        if dry_run:
            info(f"  Would update {wf_file.name} branches to [{branches_str}]")
            changed = True
            continue
        new_content = content.replace(
            f"branches: [{existing}]",
            f"branches: [{branches_str}]",
            1,
        )
        if new_content != content:
            wf_file.write_text(new_content)
            info(f"  Updated {wf_file.name} branches to [{branches_str}]")
            changed = True

    return changed


def check_release_setup(project_dir: Path) -> bool:
    """Check if release branches and config are properly set up."""
    ok = True

    for name, _ in ALL_CHANNELS:
        if name == "main":
            continue
        if _has_branch(name):
            info(f"  Branch '{name}' exists")
        else:
            # Only warn for release (required); alpha/beta are optional
            if name == "release":
                warn(f"  Branch '{name}' does not exist")
                ok = False
            else:
                info(f"  Branch '{name}' not configured (optional)")

    releaserc = _get_releaserc_path(project_dir)
    if not releaserc:
        warn("  No .releaserc file found")
        ok = False
    else:
        config, _ = _load_releaserc(releaserc)
        channels = _configured_channels(config)
        if "main" in channels and "release" in channels:
            info(f"  .releaserc channels: {', '.join(sorted(channels))}")
        else:
            warn("  .releaserc missing required channels (main + release)")
            ok = False

    return ok


def parse_channels(channels_str: str | None) -> set[str]:
    """Parse a comma-separated channels string into a set.

    Always includes 'main' and 'release'. Additional channels
    (alpha, beta) are added from the input.
    """
    result = set(DEFAULT_CHANNELS)
    if channels_str:
        for ch in channels_str.split(","):
            ch = ch.strip().lower()
            valid = {name for name, _ in ALL_CHANNELS}
            if ch in valid:
                result.add(ch)
            else:
                warn(f"  Unknown channel '{ch}' — valid: {', '.join(sorted(valid))}")
    return result


def init_release(
    project_dir: Path,
    *,
    channels_str: str | None = None,
    dry_run: bool = False,
    check_only: bool = False,
) -> int:
    """Set up release branches and configure semantic-release channels.

    Returns 0 on success, 1 on failure.
    """
    info(f"Release setup for {project_dir.name}")

    if check_only:
        ok = check_release_setup(project_dir)
        return 0 if ok else 1

    channels = parse_channels(channels_str)
    info(f"  Channels: {', '.join(sorted(channels))}")

    git_check = _run(["git", "rev-parse", "--git-dir"], cwd=project_dir)
    if git_check.returncode != 0:
        error("Not a git repository")
        return 1

    if not (project_dir / ".hyperi-ci.yaml").exists():
        warn("No .hyperi-ci.yaml found — run 'hyperi-ci init' first")

    changes_made = False

    # Step 1: Update .releaserc
    releaserc = _get_releaserc_path(project_dir)
    if releaserc:
        if _update_releaserc(releaserc, channels, dry_run=dry_run):
            changes_made = True
    else:
        warn("  No .releaserc file found — run 'hyperi-ci init' to create one")

    # Step 2: Update consumer workflow to trigger on all channel branches
    if _update_consumer_workflow(project_dir, channels, dry_run=dry_run):
        changes_made = True

    # Step 3: Create branches that don't exist
    for name, _ in ALL_CHANNELS:
        if name == "main" or name not in channels:
            continue
        if not _has_branch(name):
            if dry_run:
                info(f"  Would create branch '{name}' from main")
                changes_made = True
            else:
                result = _run(["git", "branch", name, "main"], cwd=project_dir)
                if result.returncode != 0:
                    error(f"Failed to create branch '{name}': {result.stderr.strip()}")
                    return 1
                info(f"  Created branch '{name}' from main")

                result = _run(
                    ["git", "push", "-u", "origin", name],
                    cwd=project_dir,
                )
                if result.returncode != 0:
                    error(f"Failed to push branch '{name}': {result.stderr.strip()}")
                    return 1
                info(f"  Pushed branch '{name}' to origin")
                changes_made = True
        else:
            info(f"  Branch '{name}' already exists")

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
