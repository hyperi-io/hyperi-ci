#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/sync-secrets-access.py
# Purpose:   Sync org secret repo access from config/secrets-access.yaml
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Sync GitHub org secret repo access lists from group-based config.

Usage:
    uv run scripts/sync-secrets-access.py           # default: --check
    uv run scripts/sync-secrets-access.py --check   # show drift (dry run)
    uv run scripts/sync-secrets-access.py --apply   # sync repo lists
    uv run scripts/sync-secrets-access.py --delete-legacy  # remove legacy secrets

Only manages repo lists for secrets already set to 'selected' visibility.
Secrets at 'all' or 'private' visibility must be changed manually in the
GitHub UI first (requires re-entering the secret value).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _ROOT / "config" / "secrets-access.yaml"
_ORG = "hyperi-io"

_LEGACY_SECRETS = [
    "ARTIFACTORY_CI_TOKEN",
    "ARTIFACTORY_CI_USERNAME",
    "ARTIFACTORY_PASSWORD",
    "ARTIFACTORY_TENANT_PASSWORD",
    "ARTIFACTORY_TENANT_REGISTRY",
    "ARTIFACTORY_TENANT_USERNAME",
    "ARTIFACTORY_USERNAME",
    "HS_CI_PAT",
]


def _gh_api(
    method: str,
    path: str,
    body: dict | None = None,
) -> tuple[int, str]:
    """Call GitHub API via gh CLI."""
    cmd = ["gh", "api", "--method", method, path]
    if body:
        cmd.extend(["--input", "-"])
        result = subprocess.run(
            cmd,
            input=json.dumps(body),
            capture_output=True,
            text=True,
            timeout=15,
        )
    else:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    return result.returncode, result.stdout.strip()


def _get_repo_id(repo_name: str) -> int | None:
    """Get GitHub repo ID by name."""
    full_name = f"{_ORG}/{repo_name}"
    rc, output = _gh_api("GET", f"/repos/{full_name}")
    if rc != 0:
        return None
    try:
        return json.loads(output)["id"]
    except (json.JSONDecodeError, KeyError):
        return None


def _get_secret_info(secret_name: str) -> dict | None:
    """Get secret visibility and selected repos."""
    rc, output = _gh_api(
        "GET",
        f"/orgs/{_ORG}/actions/secrets/{secret_name}",
    )
    if rc != 0:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _get_secret_repos(secret_name: str) -> set[str]:
    """Get current repo names for a selected-visibility secret."""
    rc, output = _gh_api(
        "GET",
        f"/orgs/{_ORG}/actions/secrets/{secret_name}/repositories",
    )
    if rc != 0:
        return set()
    try:
        data = json.loads(output)
        return {r["name"] for r in data.get("repositories", [])}
    except (json.JSONDecodeError, KeyError):
        return set()


def _set_secret_repos(secret_name: str, repo_ids: list[int]) -> bool:
    """Set the repo list for a selected-visibility secret."""
    rc, _ = _gh_api(
        "PUT",
        f"/orgs/{_ORG}/actions/secrets/{secret_name}/repositories",
        body={"selected_repository_ids": repo_ids},
    )
    return rc == 0


def _load_config() -> dict:
    """Load secrets-access.yaml."""
    with open(_CONFIG_FILE) as f:
        return yaml.safe_load(f)


def _resolve_groups(
    groups_config: dict[str, list[str]],
    group_names: list[str],
) -> set[str]:
    """Resolve group names to a set of repo names."""
    repos: set[str] = set()
    for name in group_names:
        if name in groups_config:
            repos.update(groups_config[name])
        else:
            print(f"  WARNING: unknown group '{name}'")
    return repos


def _check(config: dict) -> int:
    """Show drift between config and GitHub."""
    groups = config.get("groups", {})
    secrets = config.get("secrets", {})
    drift_count = 0

    print("Checking secret repo access...\n")

    for secret_name, spec in sorted(secrets.items()):
        desired_visibility = spec.get("visibility", "selected")
        desired_repos = _resolve_groups(groups, spec.get("groups", []))

        info = _get_secret_info(secret_name)
        if info is None:
            print(f"  {secret_name}: NOT FOUND on GitHub")
            drift_count += 1
            continue

        current_visibility = info.get("visibility", "unknown")

        if current_visibility != desired_visibility:
            print(
                f"  {secret_name}: visibility {current_visibility} "
                f"→ {desired_visibility} (requires UI change)"
            )
            drift_count += 1

        if current_visibility == "selected":
            current_repos = _get_secret_repos(secret_name)
            added = desired_repos - current_repos
            removed = current_repos - desired_repos
            if added or removed:
                if added:
                    print(f"  {secret_name}: add repos: {sorted(added)}")
                if removed:
                    print(f"  {secret_name}: remove repos: {sorted(removed)}")
                drift_count += 1
            else:
                print(f"  {secret_name}: OK")
        elif current_visibility != desired_visibility:
            pass  # already reported
        else:
            print(f"  {secret_name}: {current_visibility} (no repo list to check)")

    if drift_count == 0:
        print("\nAll secrets match config.")
    else:
        print(
            f"\n{drift_count} secret(s) need attention. Run --apply to fix repo lists."
        )
    return 1 if drift_count else 0


def _apply(config: dict) -> int:
    """Sync repo lists for selected-visibility secrets."""
    groups = config.get("groups", {})
    secrets = config.get("secrets", {})
    changes = 0
    errors = 0

    print("Syncing secret repo access...\n")

    # Cache repo IDs
    all_repo_names: set[str] = set()
    for spec in secrets.values():
        all_repo_names.update(
            _resolve_groups(groups, spec.get("groups", [])),
        )

    repo_ids: dict[str, int] = {}
    for name in sorted(all_repo_names):
        rid = _get_repo_id(name)
        if rid:
            repo_ids[name] = rid
        else:
            print(f"  WARNING: repo '{name}' not found, skipping")

    for secret_name, spec in sorted(secrets.items()):
        desired_repos = _resolve_groups(groups, spec.get("groups", []))

        info = _get_secret_info(secret_name)
        if info is None:
            print(f"  {secret_name}: not found on GitHub, skipping")
            continue

        current_visibility = info.get("visibility", "unknown")

        if current_visibility != "selected":
            desired = spec.get("visibility", "selected")
            if current_visibility != desired:
                print(
                    f"  {secret_name}: visibility={current_visibility}, "
                    f"needs UI change to '{desired}'"
                )
            continue

        current_repos = _get_secret_repos(secret_name)
        if current_repos == desired_repos:
            print(f"  {secret_name}: OK (no changes)")
            continue

        ids = [repo_ids[r] for r in desired_repos if r in repo_ids]
        if _set_secret_repos(secret_name, ids):
            added = desired_repos - current_repos
            removed = current_repos - desired_repos
            parts = []
            if added:
                parts.append(f"+{sorted(added)}")
            if removed:
                parts.append(f"-{sorted(removed)}")
            print(f"  {secret_name}: updated ({', '.join(parts)})")
            changes += 1
        else:
            print(f"  {secret_name}: FAILED to update")
            errors += 1

    if changes:
        print(f"\nUpdated {changes} secret(s).")
    if errors:
        print(f"{errors} error(s).")
        return 1
    if not changes:
        print("\nNo changes needed.")
    return 0


def _delete_legacy() -> int:
    """Delete legacy secrets that are no longer needed."""
    print("Deleting legacy secrets...\n")
    deleted = 0
    for name in _LEGACY_SECRETS:
        rc, _ = _gh_api("DELETE", f"/orgs/{_ORG}/actions/secrets/{name}")
        if rc == 0:
            print(f"  Deleted {name}")
            deleted += 1
        else:
            info = _get_secret_info(name)
            if info is None:
                print(f"  {name}: already gone")
            else:
                print(f"  {name}: FAILED to delete")

    print(f"\nDeleted {deleted} secret(s).")
    return 0


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Sync org secret repo access from config",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        default=True,
        help="Show drift (default)",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Sync repo lists to match config",
    )
    group.add_argument(
        "--delete-legacy",
        action="store_true",
        help="Delete legacy secrets no longer needed",
    )
    args = parser.parse_args()

    if args.delete_legacy:
        return _delete_legacy()

    config = _load_config()

    if args.apply:
        return _apply(config)
    return _check(config)


if __name__ == "__main__":
    sys.exit(main())
