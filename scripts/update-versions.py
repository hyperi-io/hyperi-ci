#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/update-versions.py
# Purpose:   Sync workflow files with config/versions.yaml SSOT
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Synchronise GitHub Actions workflow files with the central versions SSOT.

Usage:
    uv run scripts/update-versions.py                # default: --check
    uv run scripts/update-versions.py --check        # show mismatches (dry run)
    uv run scripts/update-versions.py --apply        # update workflow files
    uv run scripts/update-versions.py --latest       # check for newer versions upstream
    uv run scripts/update-versions.py --auto-update  # update, test via CI, commit or revert

Auto-update behaviour:
  - Runtimes (python, node, rust) require explicit update — never auto-bumped
  - Actions and semantic-release auto-update to latest major
  - After updating, triggers CI on test projects
  - If CI passes: commits changes
  - If CI fails: reverts and reports what failed
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_VERSIONS_FILE = _ROOT / "config" / "versions.yaml"
_WORKFLOWS_DIR = _ROOT / ".github" / "workflows"

# Maps action short names in versions.yaml to their full GitHub owner/repo
_ACTION_OWNERS: dict[str, str] = {
    "checkout": "actions/checkout",
    "setup-node": "actions/setup-node",
    "setup-go": "actions/setup-go",
    "setup-uv": "astral-sh/setup-uv",
    "cache": "actions/cache",
    "rust-toolchain": "dtolnay/rust-toolchain",
    "upload-artifact": "actions/upload-artifact",
    "download-artifact": "actions/download-artifact",
    "docker-login": "docker/login-action",
    "docker-setup-buildx": "docker/setup-buildx-action",
}


def _load_versions() -> dict:
    """Load the versions SSOT file."""
    with open(_VERSIONS_FILE) as f:
        return yaml.safe_load(f)


def _find_workflow_files() -> list[Path]:
    """Find all workflow YAML files."""
    files: list[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        files.extend(_WORKFLOWS_DIR.glob(pattern))
    return sorted(files)


def _build_replacements(versions: dict) -> list[tuple[re.Pattern, str, str]]:
    """Build regex patterns and replacements from versions config.

    Returns list of (pattern, replacement, description) tuples.
    """
    replacements: list[tuple[re.Pattern, str, str]] = []

    actions = versions.get("actions", {})
    for short_name, version in actions.items():
        owner_repo = _ACTION_OWNERS.get(short_name)
        if not owner_repo:
            continue
        owner_escaped = re.escape(owner_repo)
        pattern = re.compile(rf"({owner_escaped})@\S+")
        replacement = rf"\1@{version}"
        replacements.append((pattern, replacement, f"{owner_repo}@{version}"))

    runtimes = versions.get("runtimes", {})

    python_ver = runtimes.get("python")
    if python_ver:
        # Only match literal versions, not ${{ template expressions }}
        pattern = re.compile(r"(uv python install )(\d[\d.]*)")
        replacement = rf"\g<1>{python_ver}"
        replacements.append((pattern, replacement, f"Python {python_ver}"))

        # Also match the default value in workflow inputs
        pattern = re.compile(r'(python-version:.*\n\s+default:\s*)"([^"]+)"')
        replacement = rf'\g<1>"{python_ver}"'
        replacements.append((pattern, replacement, f"Python default {python_ver}"))

    node_ver = runtimes.get("node")
    if node_ver:
        # Only match literal versions, not ${{ template expressions }}
        pattern = re.compile(r"(node-version: )(\d[\d.]*)")
        replacement = rf"\g<1>{node_ver}"
        replacements.append((pattern, replacement, f"Node.js {node_ver}"))

        # Also match the default value in workflow inputs
        pattern = re.compile(r'(node-version:.*\n\s+default:\s*)"([^"]+)"')
        replacement = rf'\g<1>"{node_ver}"'
        replacements.append((pattern, replacement, f"Node.js default {node_ver}"))

    rust_ver = runtimes.get("rust")
    if rust_ver:
        pattern = re.compile(r"(rust-toolchain.*\n\s+default:\s*)\S+")
        replacement = rf"\g<1>{rust_ver}"
        replacements.append((pattern, replacement, f"Rust {rust_ver}"))

    sr = versions.get("semantic_release", {})
    sr_core = sr.get("core")
    if sr_core:
        pattern = re.compile(r"(semantic-release@)\S+")
        replacement = rf"\g<1>{sr_core}"
        replacements.append((pattern, replacement, f"semantic-release@{sr_core}"))

    return replacements


def _check(versions: dict) -> int:
    """Show mismatches between SSOT and workflow files."""
    replacements = _build_replacements(versions)
    files = _find_workflow_files()
    mismatches = 0

    for wf_file in files:
        content = wf_file.read_text()
        rel_path = wf_file.relative_to(_ROOT)

        for pattern, replacement, description in replacements:
            for match in pattern.finditer(content):
                expected = pattern.sub(replacement, match.group(0))
                if match.group(0) != expected:
                    line_num = content[: match.start()].count("\n") + 1
                    print(f"  {rel_path}:{line_num}: {match.group(0)} → {expected}")
                    mismatches += 1

    if mismatches == 0:
        print("All workflow files match versions.yaml")
    else:
        print(f"\n{mismatches} mismatch(es) found. Run --apply to fix.")
    return 1 if mismatches else 0


def _apply(versions: dict) -> int:
    """Update workflow files to match SSOT."""
    replacements = _build_replacements(versions)
    files = _find_workflow_files()
    total_changes = 0

    for wf_file in files:
        content = wf_file.read_text()
        original = content
        rel_path = wf_file.relative_to(_ROOT)

        for pattern, replacement, description in replacements:
            content = pattern.sub(replacement, content)

        if content != original:
            wf_file.write_text(content)
            changes = sum(
                1 for a, b in zip(original.splitlines(), content.splitlines()) if a != b
            )
            print(f"  Updated {rel_path} ({changes} line(s))")
            total_changes += changes

    if total_changes == 0:
        print("No changes needed — all files match versions.yaml")
    else:
        print(f"\nApplied {total_changes} change(s)")
    return 0


def _fix(versions: dict) -> int:
    """Apply fixes and return 1 if changes were needed (pre-commit hook mode).

    Unlike --apply (always returns 0), --fix returns 1 when files were
    modified. This tells the pre-commit framework to re-stage and retry.
    """
    replacements = _build_replacements(versions)
    files = _find_workflow_files()
    total_changes = 0

    for wf_file in files:
        content = wf_file.read_text()
        original = content
        rel_path = wf_file.relative_to(_ROOT)

        for pattern, replacement, description in replacements:
            content = pattern.sub(replacement, content)

        if content != original:
            wf_file.write_text(content)
            changes = sum(
                1 for a, b in zip(original.splitlines(), content.splitlines()) if a != b
            )
            print(f"  Fixed {rel_path} ({changes} line(s))")
            total_changes += changes

    if total_changes == 0:
        return 0

    print(
        f"\nFixed {total_changes} version mismatch(es) — files updated, please re-stage."
    )
    return 1


def _latest(versions: dict) -> int:
    """Check for newer versions available upstream via GitHub API."""
    actions = versions.get("actions", {})
    updates_available = 0

    print("Checking latest versions via GitHub API...\n")

    for short_name, current_ver in actions.items():
        owner_repo = _ACTION_OWNERS.get(short_name)
        if not owner_repo:
            continue

        if current_ver == "master":
            print(f"  {owner_repo}@master (branch pin, skipping)")
            continue

        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"/repos/{owner_repo}/releases/latest",
                    "--jq",
                    ".tag_name",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    [
                        "gh",
                        "api",
                        f"/repos/{owner_repo}/tags",
                        "--jq",
                        ".[0].name",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            latest = result.stdout.strip()
            if not latest:
                print(f"  {owner_repo}: could not determine latest")
                continue

            latest_major = latest.split(".")[0] if "." in latest else latest

            if latest_major != current_ver:
                print(
                    f"  {owner_repo}: {current_ver} → {latest_major} (latest: {latest})"
                )
                updates_available += 1
            else:
                print(f"  {owner_repo}: {current_ver} (up to date)")

        except subprocess.TimeoutExpired:
            print(f"  {owner_repo}: timeout querying GitHub API")
        except FileNotFoundError:
            print("  ERROR: 'gh' CLI not found. Install GitHub CLI.")
            return 1

    runtimes = versions.get("runtimes", {})
    print()
    for name, ver in runtimes.items():
        print(f"  {name}: {ver} (manual — check release notes)")

    sr = versions.get("semantic_release", {})
    sr_core = sr.get("core")
    if sr_core:
        try:
            result = subprocess.run(
                ["npm", "view", "semantic-release", "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            latest_sr = result.stdout.strip()
            latest_sr_major = latest_sr.split(".")[0] if latest_sr else "?"
            if latest_sr_major != sr_core:
                print(
                    f"\n  semantic-release: {sr_core} → {latest_sr_major} "
                    f"(latest: {latest_sr})"
                )
                updates_available += 1
            else:
                print(f"\n  semantic-release: {sr_core} (up to date)")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print(f"\n  semantic-release: {sr_core} (could not check)")

    if updates_available:
        print(f"\n{updates_available} update(s) available.")
        print("Edit config/versions.yaml then run --apply.")
    else:
        print("\nAll versions up to date.")
    return 0


# Auto-update skip list: these require explicit human decision
_AUTO_UPDATE_SKIP = {"python", "node", "rust"}

_TEST_PROJECTS = [
    "hyperi-io/ci-test-python-cli",
    "hyperi-io/ci-test-rust-minimal",
    "hyperi-io/ci-test-ts-simple",
    "hyperi-io/ci-test-go-simple",
]


def _get_latest_action_version(owner_repo: str) -> str | None:
    """Query GitHub API for latest release tag of an action."""
    try:
        result = subprocess.run(
            ["gh", "api", f"/repos/{owner_repo}/releases/latest", "--jq", ".tag_name"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["gh", "api", f"/repos/{owner_repo}/tags", "--jq", ".[0].name"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        latest = result.stdout.strip()
        if latest and "." in latest:
            return latest.split(".")[0]
        return latest or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _get_latest_npm_major(package: str) -> str | None:
    """Query npm for latest major version of a package."""
    try:
        result = subprocess.run(
            ["npm", "view", package, "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ver = result.stdout.strip()
        return ver.split(".")[0] if ver else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _trigger_and_wait(repo: str, timeout_minutes: int = 15) -> tuple[bool, str]:
    """Trigger CI on a test project and wait for result.

    Returns (success, message).
    """
    try:
        result = subprocess.run(
            ["gh", "workflow", "run", "ci.yml", "-R", repo],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, f"Failed to trigger: {result.stderr.strip()}"

        # Wait for the run to appear
        time.sleep(5)

        # Get the latest run ID
        result = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "-R",
                repo,
                "-w",
                "ci.yml",
                "--limit",
                "1",
                "--json",
                "databaseId,status",
                "--jq",
                ".[0]",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, f"Failed to get run: {result.stderr.strip()}"

        run_info = json.loads(result.stdout.strip())
        run_id = run_info["databaseId"]

        # Poll until complete
        deadline = time.time() + (timeout_minutes * 60)
        while time.time() < deadline:
            result = subprocess.run(
                [
                    "gh",
                    "run",
                    "view",
                    str(run_id),
                    "-R",
                    repo,
                    "--json",
                    "status,conclusion",
                    "--jq",
                    r'"\(.status) \(.conclusion)"',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout.strip()
            if "completed" in output:
                if "success" in output:
                    return True, "passed"
                return False, f"CI result: {output}"
            time.sleep(30)

        return False, "Timed out waiting for CI"

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        return False, f"Error: {e}"


def _auto_update(versions: dict) -> int:
    """Auto-update non-runtime versions, test, commit or revert."""
    print("Auto-update: checking for newer versions...\n")

    updates: dict[str, tuple[str, str]] = {}

    # Check actions (skip branch pins like @master)
    actions = versions.get("actions", {})
    for short_name, current_ver in actions.items():
        if current_ver == "master":
            continue
        owner_repo = _ACTION_OWNERS.get(short_name)
        if not owner_repo:
            continue
        latest = _get_latest_action_version(owner_repo)
        if latest and latest != current_ver:
            updates[f"actions.{short_name}"] = (current_ver, latest)
            print(f"  {owner_repo}: {current_ver} → {latest}")

    # Check semantic-release
    sr = versions.get("semantic_release", {})
    sr_core = sr.get("core")
    if sr_core:
        latest_sr = _get_latest_npm_major("semantic-release")
        if latest_sr and latest_sr != sr_core:
            updates["semantic_release.core"] = (sr_core, latest_sr)
            print(f"  semantic-release: {sr_core} → {latest_sr}")

    # Runtimes are never auto-updated
    runtimes = versions.get("runtimes", {})
    for name in _AUTO_UPDATE_SKIP:
        ver = runtimes.get(name)
        if ver:
            print(f"  {name}: {ver} (manual — skipped)")

    if not updates:
        print("\nNo auto-updates available.")
        return 0

    print(f"\n{len(updates)} update(s) to apply.")

    # Back up current versions.yaml
    original_yaml = _VERSIONS_FILE.read_text()
    original_workflows: dict[str, str] = {}
    for wf in _find_workflow_files():
        original_workflows[str(wf)] = wf.read_text()

    # Update versions.yaml
    updated_versions = dict(versions)
    for key, (_, new_ver) in updates.items():
        parts = key.split(".")
        target = updated_versions
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = new_ver

    # Write updated versions.yaml
    with open(_VERSIONS_FILE) as f:
        yaml_content = f.read()
    for key, (old_ver, new_ver) in updates.items():
        yaml_content = yaml_content.replace(
            f": {old_ver}",
            f": {new_ver}",
            1,
        )
    _VERSIONS_FILE.write_text(yaml_content)

    # Apply to workflow files
    print("\nApplying to workflow files...")
    updated_versions_loaded = _load_versions()
    _apply(updated_versions_loaded)

    # Trigger CI on test projects
    print("\nTriggering CI on test projects...")
    failures: list[str] = []
    for repo in _TEST_PROJECTS:
        print(f"  {repo}...", end=" ", flush=True)
        ok, msg = _trigger_and_wait(repo, timeout_minutes=15)
        if ok:
            print("passed")
        else:
            print(f"FAILED ({msg})")
            failures.append(f"{repo}: {msg}")

    if failures:
        print(f"\n{len(failures)} test project(s) failed:")
        for f in failures:
            print(f"  {f}")
        print("\nReverting all changes...")
        _VERSIONS_FILE.write_text(original_yaml)
        for path_str, content in original_workflows.items():
            Path(path_str).write_text(content)
        print("Reverted. Fix the issues and try again.")
        return 1

    print("\nAll test projects passed. Changes applied:")
    for key, (old_ver, new_ver) in updates.items():
        print(f"  {key}: {old_ver} → {new_ver}")
    print("\nReview and commit when ready.")
    return 0


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Sync workflow files with config/versions.yaml",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        default=True,
        help="Show mismatches (default)",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Update workflow files to match SSOT",
    )
    group.add_argument(
        "--latest",
        action="store_true",
        help="Check for newer versions upstream",
    )
    group.add_argument(
        "--auto-update",
        action="store_true",
        help="Update non-runtime versions, test via CI, commit or revert",
    )
    group.add_argument(
        "--fix",
        action="store_true",
        help="Apply fixes and exit 1 if changes were made (for pre-commit hooks)",
    )
    args = parser.parse_args()

    versions = _load_versions()

    if args.auto_update:
        return _auto_update(versions)
    if args.latest:
        return _latest(versions)
    if args.fix:
        return _fix(versions)
    if args.apply:
        return _apply(versions)
    return _check(versions)


if __name__ == "__main__":
    sys.exit(main())
