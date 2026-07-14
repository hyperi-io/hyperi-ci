#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/update-versions.py
# Purpose:   Sync workflow files with config/versions.yaml SSOT
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Pin GitHub Actions across the pipeline from the central versions SSOT.

This is the /deps tool for hyperi-ci. Actions pin to a commit SHA with a
`# <version>` comment (a tag can be force-moved, a SHA can't); the pre-commit
hook enforces it. Scans both .github/workflows/ and .github/actions/. Policy
+ the Renovate split: docs/CI-DEPENDENCIES.md.

Usage:
    uv run scripts/update-versions.py                # default: --check
    uv run scripts/update-versions.py --check        # show drift (dry run)
    uv run scripts/update-versions.py --apply        # rewrite pipeline to SSOT
    uv run scripts/update-versions.py --latest       # report newest release >=7d old
    uv run scripts/update-versions.py --auto-update  # bump SSOT, test via CI, commit/revert

Update behaviour:
  - Actions resolve to the newest release that has aged past the 7-day
    cooldown, within the current major (major bumps are a manual edit).
  - Branch refs (rust-toolchain@master) pin the newest master commit >=7d old.
  - Runtimes (python, node, rust) require explicit update — never auto-bumped.
  - --auto-update applies the bumps then validates LOCALLY (YAML re-parse,
    SSOT sync check, the pytest workflow gates); reverts on local failure.
    It deliberately does NOT trigger remote CI: the ci-test-* projects
    reference the reusable workflows @main, so a remote run validates main,
    not the unpushed bumps — and it reverted good bumps on unrelated remote
    failures. Real E2E belongs to the branch-mode rehearsal/sweep (see
    docs/plans/2026-07-branch-mode/PLAN.md decisions 4, 5 and 7).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_VERSIONS_FILE = _ROOT / "config" / "versions.yaml"
_WORKFLOWS_DIR = _ROOT / ".github" / "workflows"
_ACTIONS_DIR = _ROOT / ".github" / "actions"

# How long a release must have existed before we'll pin it. Mirrors the org
# Renovate preset's `minimumReleaseAge` — a release sitting untouched for a
# week is far less likely to be a compromised/yanked supply-chain attack.
_COOLDOWN_DAYS = 7

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
    "ghcr-cleanup": "dataaxiom/ghcr-cleanup-action",
}


def _load_versions() -> dict[str, Any]:
    """Load the versions SSOT file."""
    with open(_VERSIONS_FILE) as f:
        return yaml.safe_load(f)


def _find_workflow_files() -> list[Path]:
    """Find every pipeline YAML — workflows AND composite actions.

    Composite actions under `.github/actions/*/action.yml` pin third-party
    actions too (setup-node, etc.), so they must be scanned or they'd drift
    unpinned — the gap that hid the unpinned refs during the deps review.
    """
    files: list[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        files.extend(_WORKFLOWS_DIR.glob(pattern))
    if _ACTIONS_DIR.is_dir():
        for pattern in ("**/action.yml", "**/action.yaml"):
            files.extend(_ACTIONS_DIR.glob(pattern))
    return sorted(files)


def _parse_semver(tag: str) -> tuple[int, int, int] | None:
    """Parse `v1.2.3` / `1.2.3` to a tuple. None for anything else.

    Rejects suffixed tags like `v3.1.0-node20` — those are backports, not
    the canonical latest, and must never win selection.
    """
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", tag.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _select_pinned_release(
    releases: list[dict[str, Any]],
    now: datetime,
    cooldown_days: int = _COOLDOWN_DAYS,
    major: int | None = None,
) -> dict[str, Any] | None:
    """Pick the highest-semver release that has aged past the cooldown.

    Highest semver, NOT newest-published: GitHub republishes old backports
    (e.g. download-artifact `v3.1.0-node20`) with recent dates, so ordering
    by publish date picks the wrong one. Skips drafts, prereleases,
    non-semver tags, and — timestamp-required posture — anything without a
    `published_at`. With `major` set, stays within that major so a surprise
    major bump never auto-lands (those are a deliberate edit). Returns the
    chosen release dict or None.
    """
    cutoff = now - timedelta(days=cooldown_days)
    best: dict[str, Any] | None = None
    best_ver: tuple[int, int, int] | None = None
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        ts = rel.get("published_at")
        if not ts:
            continue
        if datetime.fromisoformat(ts.replace("Z", "+00:00")) > cutoff:
            continue
        ver = _parse_semver(rel.get("tag_name", ""))
        if ver is None:
            continue
        if major is not None and ver[0] != major:
            continue
        if best_ver is None or ver > best_ver:
            best_ver, best = ver, rel
    return best


def _gh_json(path: str) -> Any:
    """GET a GitHub API path, parsed as JSON. None on any failure.

    Returns the parsed JSON (typically dict or list) or None. Typed `Any`
    because the JSON shape varies per endpoint - callers `isinstance`-guard
    before use.
    """
    try:
        result = subprocess.run(
            ["gh", "api", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None


def _resolve_tag_sha(owner_repo: str, tag: str) -> str | None:
    """Resolve a tag to its commit SHA (dereferencing annotated tags)."""
    ref = _gh_json(f"/repos/{owner_repo}/git/ref/tags/{tag}")
    if not isinstance(ref, dict):
        return None
    obj: Any = cast("dict[str, Any]", ref).get("object", {})
    if obj.get("type") == "tag":
        # annotated tag → deref to the commit it points at
        tag_obj = _gh_json(f"/repos/{owner_repo}/git/tags/{obj.get('sha')}")
        if isinstance(tag_obj, dict):
            inner: Any = cast("dict[str, Any]", tag_obj).get("object", {})
            return inner.get("sha")
    return obj.get("sha")


def _resolve_branch_sha(
    owner_repo: str, branch: str, now: datetime, cooldown_days: int = _COOLDOWN_DAYS
) -> str | None:
    """Pin a branch ref (e.g. rust-toolchain@master) to its newest commit
    that is older than the cooldown — no releases to gate on, so use the
    commit date instead."""
    commits = _gh_json(f"/repos/{owner_repo}/commits?sha={branch}&per_page=50")
    if not isinstance(commits, list):
        return None
    cutoff = now - timedelta(days=cooldown_days)
    for commit in cast("list[dict[str, Any]]", commits):
        date_str = commit.get("commit", {}).get("committer", {}).get("date")
        if not date_str:
            continue
        committed = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if committed <= cutoff:
            return commit.get("sha")
    return None


def _pinned_spec_for(short_name: str, current: object, now: datetime) -> dict | None:
    """Resolve {version, sha} for an action under the cooldown rule.

    Branch pins (version == "master") track the branch HEAD ≥ cooldown.
    Everything else picks the newest release ≥ cooldown and resolves its
    tag to a SHA. Returns None if nothing eligible / lookups fail.
    """
    owner_repo = _ACTION_OWNERS.get(short_name)
    if not owner_repo:
        return None

    cur_version = current.get("version") if isinstance(current, dict) else current

    if cur_version == "master":
        sha = _resolve_branch_sha(owner_repo, "master", now)
        return {"version": "master", "sha": sha} if sha else None

    releases = _gh_json(f"/repos/{owner_repo}/releases?per_page=30")
    if not isinstance(releases, list):
        return None
    releases = cast("list[dict[str, Any]]", releases)
    # Stay within the current major — major bumps are a deliberate edit.
    cur_semver = _parse_semver(str(cur_version)) if cur_version else None
    major = cur_semver[0] if cur_semver else None
    chosen = _select_pinned_release(releases, now, major=major)
    if not chosen:
        return None
    tag = chosen["tag_name"]
    sha = _resolve_tag_sha(owner_repo, tag)
    return {"version": tag, "sha": sha} if sha else None


def _action_ref(spec: object) -> tuple[str, str]:
    """Resolve an action spec from versions.yaml to (ref, comment).

    New format — `{version: v6.0.2, sha: <sha>}` — pins the SHA with a
    `# <version>` comment (supply-chain hardening: a tag can move, a SHA
    can't). Legacy flat string — `v6` — pins the tag, no comment
    (back-compat; lets a value be migrated incrementally).
    """
    if isinstance(spec, dict):
        sha = spec.get("sha")
        version = spec.get("version", "")
        if sha:
            return str(sha), f" # {version}" if version else ""
        return str(version), ""
    return str(spec), ""


def _build_replacements(versions: dict) -> list[tuple[re.Pattern, str, str]]:
    """Build regex patterns and replacements from versions config.

    Returns list of (pattern, replacement, description) tuples.
    """
    replacements: list[tuple[re.Pattern, str, str]] = []

    actions = versions.get("actions", {})
    for short_name, spec in actions.items():
        owner_repo = _ACTION_OWNERS.get(short_name)
        if not owner_repo:
            continue
        ref, comment = _action_ref(spec)
        owner_escaped = re.escape(owner_repo)
        # Consume the ref plus any trailing `# comment` so re-runs are
        # idempotent and a stale multi-token comment is fully replaced.
        pattern = re.compile(rf"({owner_escaped})@\S+(?:[ \t]*#[^\n]*)?")
        replacement = rf"\1@{ref}{comment}"
        replacements.append((pattern, replacement, f"{owner_repo}@{ref}{comment}"))

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
        # Negative lookbehind: don't match inside a longer name such as the
        # setup-semantic-release@main action ref (only the bare npm package).
        pattern = re.compile(r"(?<![\w-])(semantic-release@)\S+")
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

    print(f"Checking latest versions (>= {_COOLDOWN_DAYS}-day cooldown)...\n")
    now = datetime.now(UTC)

    for short_name, current in actions.items():
        owner_repo = _ACTION_OWNERS.get(short_name)
        if not owner_repo:
            continue
        cur_version = current.get("version") if isinstance(current, dict) else current
        cur_sha = current.get("sha") if isinstance(current, dict) else None

        spec = _pinned_spec_for(short_name, current, now)
        if not spec:
            print(f"  {owner_repo}: {cur_version} (nothing aged past cooldown)")
            continue
        if spec["version"] != cur_version or spec["sha"] != cur_sha:
            print(
                f"  {owner_repo}: {cur_version} → {spec['version']} ({spec['sha'][:12]})"
            )
            updates_available += 1
        else:
            print(f"  {owner_repo}: {cur_version} (up to date)")

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
                encoding="utf-8",
                errors="replace",
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


def _get_latest_npm_major(package: str) -> str | None:
    """Query npm for latest major version of a package."""
    try:
        result = subprocess.run(
            ["npm", "view", package, "version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        ver = result.stdout.strip()
        return ver.split(".")[0] if ver else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _validate_locally() -> list[str]:
    """Validate the applied bumps with LOCAL gates. Returns failure messages.

    Three gates, cheapest first — all offline apart from nothing:
      1. every pipeline YAML still parses,
      2. files match the SSOT (--check clean — a bad regex rewrite shows
         here as drift or a mangled ref),
      3. the pytest workflow gates (consistency + interface tests) pass.

    This replaces the old remote-trigger flow, which validated @main rather
    than the local bumps (the ci-test-* callers pin @main) and reverted good
    bumps on unrelated remote failures.
    """
    failures: list[str] = []

    for wf_file in _find_workflow_files():
        try:
            yaml.safe_load(wf_file.read_text())
        except yaml.YAMLError as e:
            failures.append(f"YAML parse: {wf_file.relative_to(_ROOT)}: {e}")
    if failures:
        return failures  # unparseable files make the later gates meaningless

    if _check(_load_versions()) != 0:
        return ["SSOT sync: --check found drift after --apply"]

    result = subprocess.run(
        ["uv", "run", "pytest", "tests/unit", "-k", "workflow", "-q"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stdout.splitlines()[-15:])
        failures.append(
            f"workflow pytest gates failed (exit {result.returncode}):\n{tail}"
        )

    return failures


def _set_action_spec_in_yaml(text: str, short_name: str, version: str, sha: str) -> str:
    """Rewrite one action's `version:`/`sha:` lines in versions.yaml in place.

    Block-scoped so comments and other actions are untouched — yaml.safe_dump
    would nuke the file's comments, so we edit the lines directly.
    """
    out: list[str] = []
    in_block = False
    for line in text.splitlines(keepends=True):
        if re.match(rf"^  {re.escape(short_name)}:\s*$", line):
            in_block = True
            out.append(line)
            continue
        if in_block:
            if re.match(r"^    version:\s", line):
                out.append(f"    version: {version}\n")
                continue
            if re.match(r"^    sha:\s", line):
                out.append(f"    sha: {sha}\n")
                continue
            if re.match(r"^  \S", line):  # next 2-space key/comment → block ended
                in_block = False
        out.append(line)
    return "".join(out)


def _auto_update(versions: dict) -> int:
    """Auto-update actions + semantic-release, validate locally, revert on fail.

    Actions resolve to the newest release past the 7-day cooldown, within
    their current major (major bumps stay a manual edit). Runtimes never
    auto-bump. Validation is LOCAL (see _validate_locally) — remote E2E is
    the branch-mode rehearsal's job, not this script's.
    """
    print("Auto-update: resolving releases past the cooldown...\n")
    now = datetime.now(UTC)

    actions = versions.get("actions", {})
    action_updates: dict[str, dict] = {}
    for short_name, current in actions.items():
        if short_name not in _ACTION_OWNERS:
            continue
        cur_version = current.get("version") if isinstance(current, dict) else current
        cur_sha = current.get("sha") if isinstance(current, dict) else None
        spec = _pinned_spec_for(short_name, current, now)
        if spec and (spec["version"] != cur_version or spec["sha"] != cur_sha):
            action_updates[short_name] = spec
            print(
                f"  {_ACTION_OWNERS[short_name]}: {cur_version} → "
                f"{spec['version']} ({spec['sha'][:12]})"
            )

    sr = versions.get("semantic_release", {})
    sr_core = sr.get("core")
    sr_update: tuple[str, str] | None = None
    if sr_core:
        latest_sr = _get_latest_npm_major("semantic-release")
        if latest_sr and latest_sr != sr_core:
            sr_update = (sr_core, latest_sr)
            print(f"  semantic-release: {sr_core} → {latest_sr}")

    runtimes = versions.get("runtimes", {})
    for name in _AUTO_UPDATE_SKIP:
        if runtimes.get(name):
            print(f"  {name}: {runtimes[name]} (manual — skipped)")

    if not action_updates and not sr_update:
        print("\nNo auto-updates available.")
        return 0

    total = len(action_updates) + (1 if sr_update else 0)
    print(f"\n{total} update(s) to apply.")

    original_yaml = _VERSIONS_FILE.read_text()
    original_workflows = {str(wf): wf.read_text() for wf in _find_workflow_files()}

    yaml_content = original_yaml
    for short_name, spec in action_updates.items():
        yaml_content = _set_action_spec_in_yaml(
            yaml_content, short_name, spec["version"], spec["sha"]
        )
    if sr_update:
        yaml_content = re.sub(
            r'(?m)^(  core:\s*")[^"]*(")', rf"\g<1>{sr_update[1]}\g<2>", yaml_content
        )
    _VERSIONS_FILE.write_text(yaml_content)

    print("\nApplying to pipeline files...")
    _apply(_load_versions())

    print("\nValidating locally (YAML parse, SSOT sync, workflow pytest gates)...")
    failures = _validate_locally()

    if failures:
        print(f"\n{len(failures)} local gate(s) failed:")
        for f in failures:
            print(f"  {f}")
        print("\nReverting all changes...")
        _VERSIONS_FILE.write_text(original_yaml)
        for path_str, content in original_workflows.items():
            Path(path_str).write_text(content)
        print("Reverted. Fix the issues and try again.")
        return 1

    print("\nLocal gates passed. Review and commit when ready.")
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
        help="Update non-runtime versions, validate locally, revert on fail",
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
