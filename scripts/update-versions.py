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
+ the Renovate split: docs/dependencies/deps-pinning.md.

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


# Marker that anchors a mirrored tool pin, e.g.
#
#     # hyperi-ci:pin tools.gitleaks
#     _GITLEAKS_VERSION = "v8.30.1"
#
#     # hyperi-ci:pin tools.osv-scanner
#     default: v2.4.0
#
# One explicit marker beats a per-tool regex guessing at each file's shape: it
# reads the same in Python and YAML, survives the line being reworded, and lets
# several tools share one file without a `default:` pattern rewriting all of
# them to the same version. An unmarked pin is REPORTED, never silently missed.
_PIN_MARKER = r"#\s*hyperi-ci:pin\s+tools\.{name}\s*\n"

# Tag on problems that --apply CANNOT repair (it has nothing to anchor a rewrite
# to). Callers test for this literal instead of pattern-matching prose, so the
# advice cannot silently rot when a message is reworded.
_UNFIXABLE = "NOT-AUTO-FIXABLE"


def _tool_pin_pattern(name: str) -> re.Pattern[str]:
    """Match the version token on the line following this tool's pin marker.

    Requires a `=` or `:` (with optional opening quote) between the marker and
    the version, so a digit inside an identifier - `_SHA256 = ...` - can't be
    mistaken for the version.
    """
    marker = _PIN_MARKER.format(name=re.escape(name))
    return re.compile(rf'({marker}[^\n]*?[=:]\s*"?)(v?\d[\w.+-]*)')


def _load_versions() -> dict[str, Any]:
    """Load the versions SSOT file."""
    # Explicit encoding per the project rule: the default follows the locale,
    # and this file DOES contain non-ASCII (the licence header em-dashes).
    with open(_VERSIONS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _tool_pins(
    versions: dict,
) -> tuple[list[tuple[Path, re.Pattern[str], str, str]], list[str]]:
    """Resolve `tools:` to ([(path, pattern, wanted version, name)], problems).

    External CLI tools are consumed from Python / composite-action source
    rather than a `uses:` line, so each pin is rewritten only in the one file
    its `pin:` key names.

    A malformed entry is RETURNED AS A REASON, never merely warned about and
    never reduced to a bare count. Two reasons:
      - warn-and-continue dropped the tool out of every downstream check, so
        renaming a pin file without updating `pin:` left the gate green while
        the pin stopped being enforced;
      - a count cannot tell a caller WHICH failure it was, so --check could not
        tell "run --apply" (fixable drift) apart from "fix this by hand"
        (a broken path), and sent everyone to a command that cannot help.
    """
    out: list[tuple[Path, re.Pattern[str], str, str]] = []
    problems: list[str] = []
    for name, spec in (versions.get("tools") or {}).items():
        if not isinstance(spec, dict):
            problems.append(f"  tools.{name}: not a mapping [{_UNFIXABLE}]")
            continue
        version, pin = spec.get("version"), spec.get("pin")
        if not version or not pin:
            problems.append(
                f"  tools.{name}: needs both `version:` and `pin:` [{_UNFIXABLE}]"
            )
            continue
        path = _ROOT / pin
        if not path.is_file():
            problems.append(
                f"  tools.{name}: `pin:` file does not exist: {pin} [{_UNFIXABLE}]"
            )
            continue
        out.append((path, _tool_pin_pattern(name), str(version), name))
    return out, problems


def _pin_replacement(version: str) -> str:
    """Build the re.sub replacement that swaps in `version`, keeping the prefix.

    This is a REPLACEMENT, not a pattern: only a backslash is special here, so
    re.escape would be the wrong tool (it would insert a literal `v2\\.4\\.0`).
    """
    return r"\g<1>" + version.replace("\\", "\\\\")


def _tool_mismatches(versions: dict) -> list[str]:
    """Report tool pins that disagree with the SSOT, or that we can't find.

    A pattern matching NOTHING is reported, not ignored: silently rewriting
    zero lines is how a pin drifts for nine months while the check stays green.
    """
    pins, problems = _tool_pins(versions)
    for path, pattern, version, name in pins:
        content = path.read_text(encoding="utf-8")
        rel_path = path.relative_to(_ROOT)
        matches = list(pattern.finditer(content))
        if not matches:
            problems.append(
                f"  {rel_path}: no `# hyperi-ci:pin tools.{name}` marker found - "
                f"{name} is no longer being kept in step [{_UNFIXABLE}]"
            )
            continue
        for match in matches:
            # Report the version TOKEN, not the whole match: the match spans the
            # marker line, so echoing it prints a multi-line mess and points the
            # line number at the marker rather than the pin.
            if match.group(2) != version:
                line_num = content[: match.start(2)].count("\n") + 1
                problems.append(
                    f"  {rel_path}:{line_num}: {name} {match.group(2)} → {version}"
                )
    return problems


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
    minor: int | None = None,
) -> dict[str, Any] | None:
    """Pick the highest-semver release that has aged past the cooldown.

    Highest semver, NOT newest-published: GitHub republishes old backports
    (e.g. download-artifact `v3.1.0-node20`) with recent dates, so ordering
    by publish date picks the wrong one. Skips drafts, prereleases,
    non-semver tags, and — timestamp-required posture — anything without a
    `published_at`. With `major` set, stays within that major so a surprise
    major bump never auto-lands (those are a deliberate edit). With `minor`
    set, also stays within that minor — see _compat_clamp for why 0.x needs it.
    Returns the chosen release dict or None.
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
        if minor is not None and ver[1] != minor:
            continue
        if best_ver is None or ver > best_ver:
            best_ver, best = ver, rel
    return best


def _compat_clamp(version: str) -> tuple[int | None, int | None]:
    """Return the (major, minor) clamp that keeps an auto-bump compatible.

    For 1.0.0+ the major is the compatibility axis, so clamping the major is
    enough. For **0.x the MINOR is the compatibility axis** (semver §4: anything
    may change at any time; 0.20 -> 0.21 is a breaking bump), so clamping only
    the major there is exactly backwards - it BLOCKS the safe 0.20.2 -> 1.0.0
    move while WAVING THROUGH the breaking 0.20 -> 0.21 one.

    cargo-deny (0.20.2) and cargo-audit (v0.22.2) are both 0.x, so this is live,
    not theoretical.
    """
    parsed = _parse_semver(version)
    if parsed is None:
        return None, None
    if parsed[0] == 0:
        return 0, parsed[1]
    return parsed[0], None


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
        content = wf_file.read_text(encoding="utf-8")
        rel_path = wf_file.relative_to(_ROOT)

        for pattern, replacement, _description in replacements:
            for match in pattern.finditer(content):
                expected = pattern.sub(replacement, match.group(0))
                if match.group(0) != expected:
                    line_num = content[: match.start()].count("\n") + 1
                    print(f"  {rel_path}:{line_num}: {match.group(0)} → {expected}")
                    mismatches += 1

    tool_problems = _tool_mismatches(versions)
    for problem in tool_problems:
        print(problem)
    mismatches += len(tool_problems)

    if mismatches == 0:
        print("All workflow files and tool pins match versions.yaml")
        return 0

    print(f"\n{mismatches} mismatch(es) found.")
    # Don't blanket-advise --apply: a drifted VERSION is auto-fixable, but a
    # missing marker or a broken `pin:` path is not - --apply has nothing to
    # anchor the rewrite to. Sending someone to a command that cannot help is
    # how a real problem gets mistaken for a flaky tool.
    #
    # Keyed off _UNFIXABLE, not off prose: the first cut matched substrings that
    # `_tool_pins` never actually emitted downstream, so the branch was dead and
    # every malformed entry got the "run --apply" advice this comment exists to
    # prevent. A marker in the data beats pattern-matching your own messages.
    if any(_UNFIXABLE in p for p in tool_problems):
        print("  Version drift: run --apply.")
        print("  Anything marked NOT-AUTO-FIXABLE: fix by hand, --apply cannot.")
    else:
        print("  Run --apply to fix.")
    return 1


def _rewrite_to_ssot(versions: dict, *, verb: str) -> tuple[int, int]:
    """Rewrite every action ref AND tool pin to match the SSOT.

    Returns (lines_changed, unenforceable) - the second being pins the SSOT
    declares but that could NOT be rewritten (marker gone, `pin:` file missing).
    That count is NOT cosmetic: an unenforceable pin is a pin nobody is holding,
    which is the whole failure this design exists to prevent. Callers must treat
    it as failure, not as a warning they scroll past.

    ONE rewrite path, shared by --apply and --fix. They previously carried
    near-identical copies of the workflow loop, which is how --fix (the
    pre-commit hook, i.e. the thing that actually ENFORCES the SSOT) ended up
    without the tool-pin half.
    """
    replacements = _build_replacements(versions)
    total_changes = 0

    def _write(path: Path, before: str, after: str) -> int:
        if after == before:
            return 0
        path.write_text(after, encoding="utf-8", newline="\n")
        changed = sum(
            1 for a, b in zip(before.splitlines(), after.splitlines()) if a != b
        )
        print(f"  {verb} {path.relative_to(_ROOT)} ({changed} line(s))")
        return changed

    for wf_file in _find_workflow_files():
        original = wf_file.read_text(encoding="utf-8")
        content = original
        for pattern, replacement, _description in replacements:
            content = pattern.sub(replacement, content)
        total_changes += _write(wf_file, original, content)

    pins, problems = _tool_pins(versions)
    unenforceable = len(problems)
    for problem in problems:
        print(f"  error:{problem.lstrip()}")
    for path, pattern, version, name in pins:
        original = path.read_text(encoding="utf-8")
        if not pattern.search(original):
            print(
                f"  error: {path.relative_to(_ROOT)}: no `# hyperi-ci:pin"
                f" tools.{name}` marker found - {name} is not being kept in step"
            )
            unenforceable += 1
            continue
        total_changes += _write(
            path, original, pattern.sub(_pin_replacement(version), original)
        )

    return total_changes, unenforceable


def _report_unenforceable(count: int) -> None:
    """Explain why an unenforceable pin is a hard failure, not a nag."""
    print(
        f"\n{count} tool pin(s) in config/versions.yaml cannot be enforced.\n"
        "  Nothing is holding those versions: they will drift silently, which is\n"
        "  exactly how the gitleaks pin sat 9 versions stale.\n"
        "  fix: restore the `# hyperi-ci:pin tools.<name>` marker above the line\n"
        "       carrying the version, or correct the entry's `pin:` path."
    )


def _apply(versions: dict) -> int:
    """Update workflows, composites and tool pins to match SSOT."""
    total_changes, unenforceable = _rewrite_to_ssot(versions, verb="Updated")
    if total_changes == 0:
        print("No changes needed — all files match versions.yaml")
    else:
        print(f"\nApplied {total_changes} change(s)")
    if unenforceable:
        # --apply is the "make it so" verb, so it still rewrites what it can -
        # but it must not exit 0 and imply the SSOT is now honoured.
        _report_unenforceable(unenforceable)
        return 1
    return 0


def _fix(versions: dict) -> int:
    """Apply fixes and return 1 if changes were needed (pre-commit hook mode).

    Unlike --apply, --fix returns 1 when files were modified. This tells the
    pre-commit framework to re-stage and retry.

    It ALSO returns 1 for an unenforceable pin, which --fix cannot repair by
    rewriting. --check and --fix must agree: --check is not wired into CI
    anywhere, so this hook is the ONLY automated gate - if it waves through a
    deleted marker, nothing else catches it.
    """
    total_changes, unenforceable = _rewrite_to_ssot(versions, verb="Fixed")
    if unenforceable:
        _report_unenforceable(unenforceable)
        return 1
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
    lookup_failures = 0

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

    tools = versions.get("tools") or {}
    if tools:
        print()
    for name, spec in tools.items():
        if not isinstance(spec, dict):
            continue
        cur_version, repo = spec.get("version"), spec.get("repo")
        if not repo:
            print(f"  {name}: {cur_version} (no `repo:` — cannot check)")
            continue
        # Resolve through the SAME helper --auto-update uses. Reporting and
        # bumping must never drift apart: when this loop had its own copy of the
        # resolution it missed tag_prefix, so cargo-audit read as "nothing aged
        # past cooldown" while --auto-update saw it fine.
        latest_tag, status = _latest_tool_release(spec, now)
        # Name the TOOL, not the repo: rustsec/rustsec hosts four pinned crates,
        # so "rustsec/rustsec: v0.22.2" is ambiguous.
        label = f"{name} ({repo})" if str(spec.get("tag_prefix") or "") else repo
        if status == "ok":
            print(f"  {label}: {cur_version} → {latest_tag}")
            updates_available += 1
        elif status == "lookup-failed":
            # Never render a failed lookup as "up to date" - that is a silent
            # skip wearing a green hat.
            print(f"  {label}: {cur_version} (COULD NOT CHECK — treat as unknown)")
            lookup_failures += 1
        elif status == "no-candidate":
            print(f"  {label}: {cur_version} (nothing aged past cooldown)")
        else:
            print(f"  {label}: {cur_version} (up to date)")

    runtimes = versions.get("runtimes", {})
    print()
    for name, ver in runtimes.items():
        print(f"  {name}: {ver} (manual — check release notes)")

    _report_watchlist(versions)

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
    elif not lookup_failures:
        print("\nAll versions up to date.")
    if lookup_failures:
        # "All up to date" would be a lie when we could not reach upstream.
        print(
            f"\n{lookup_failures} tool(s) COULD NOT BE CHECKED (API error / rate"
            " limit?) — their status is unknown, not current. Re-run before"
            " trusting this report."
        )
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
            yaml.safe_load(wf_file.read_text(encoding="utf-8"))
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


def _tool_releases(spec: dict, releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Narrow a repo's releases to THIS tool's, with tags normalised to semver.

    A monorepo (rustsec/rustsec) tags every crate as `<crate>/vX.Y.Z`, so an
    unfiltered scan mixes crates together, and `_parse_semver` rejects the
    prefixed tag outright - the tool would look permanently up to date while
    actually being unmanaged. `tag_prefix` selects the right crate and strips
    the prefix so the usual semver + cooldown logic applies unchanged.
    """
    prefix = str(spec.get("tag_prefix") or "")
    if not prefix:
        return releases
    out: list[dict[str, Any]] = []
    for rel in releases:
        tag = str(rel.get("tag_name") or "")
        if tag.startswith(prefix):
            out.append({**rel, "tag_name": tag[len(prefix) :]})
    return out


def _report_watchlist(versions: dict) -> None:
    """Print `watch:` - upstream capabilities we want but that are not ready.

    Surfaced on every --latest run ON PURPOSE. A "revisit this when upstream
    stabilises" decision that lives only in a code comment is a decision nobody
    revisits; printing it at the moment someone is already updating deps is the
    cheapest place to make it resurface.
    """
    watch = versions.get("watch") or {}
    if not watch:
        return
    print("\nWatchlist — recheck these while you are updating deps:")
    for name, spec in watch.items():
        if not isinstance(spec, dict):
            continue
        issue = f" (#{spec['issue']})" if spec.get("issue") else ""
        print(f"  {name}{issue}: {str(spec.get('what', '')).strip()}")
        # blocked_by carries the REASON. Declaring it and never printing it is
        # how a watchlist decays into a list of nags nobody can evaluate.
        if spec.get("blocked_by"):
            print(f"    blocked by: {' '.join(str(spec['blocked_by']).split())}")
        if spec.get("gate"):
            print(f"    ready when: {str(spec['gate']).strip()}")


def _latest_tool_release(spec: dict, now: datetime) -> tuple[str | None, str]:
    """Newest compatible release for a `tools:` entry, past cooldown.

    Returns (tag_or_None, status) where status is one of `ok` (tag is a real
    upgrade), `current`, `no-candidate` (nothing aged past the cooldown within
    the compatibility clamp), or `lookup-failed`.

    The status is NOT decoration. Collapsing all of these into a bare None made
    --latest render an API failure as "(up to date)" - so a rate-limited `gh`
    reported every tool green, which is the same silent-skip shape as a pin that
    nobody enforces.
    """
    repo, cur_version = spec.get("repo"), spec.get("version")
    if not repo or not cur_version:
        return None, "lookup-failed"
    releases = _gh_json(f"/repos/{repo}/releases?per_page=100")
    if not isinstance(releases, list):
        return None, "lookup-failed"
    releases = _tool_releases(spec, releases)
    cur = _parse_semver(str(cur_version))
    clamp_major, clamp_minor = _compat_clamp(str(cur_version))
    best = _select_pinned_release(releases, now, major=clamp_major, minor=clamp_minor)
    if not best:
        return None, "no-candidate"
    tag = best.get("tag_name")
    if not tag or tag == cur_version:
        return None, "current"
    # NEWER only, never merely different. `_select_pinned_release` returns the
    # highest release PAST THE COOLDOWN, so pinning a release younger than that
    # (which is itself a policy breach) makes the best aged candidate look like
    # an "update" - and --auto-update would silently roll the tool BACKWARDS.
    best_ver = _parse_semver(tag)
    if cur and best_ver and best_ver <= cur:
        return None, "current"
    # Preserve the SSOT's own spelling: cargo-deny tags have no leading `v` and
    # the download URL is built from this string verbatim, so re-adding one
    # would 404.
    if not str(cur_version).startswith("v") and tag.startswith("v"):
        tag = tag[1:]
    return tag, "ok"


def _set_tool_version_in_yaml(text: str, name: str, version: str) -> str:
    """Rewrite one tool's `version:` line inside the `tools:` block.

    Block-scoped like _set_action_spec_in_yaml, and additionally anchored to
    the `tools:` section: an action and a tool could share a short name, and
    yaml.safe_dump would strip every comment in the file.
    """
    out: list[str] = []
    in_tools = False
    in_block = False
    for line in text.splitlines(keepends=True):
        if re.match(r"^tools:\s*$", line):
            in_tools = True
            out.append(line)
            continue
        if in_tools and re.match(r"^\S", line):  # next top-level key ends tools:
            in_tools = False
            in_block = False
        if in_tools:
            if re.match(rf"^  {re.escape(name)}:\s*$", line):
                in_block = True
                out.append(line)
                continue
            if in_block:
                if re.match(r"^    version:\s", line):
                    out.append(f"    version: {version}\n")
                    continue
                if re.match(r"^  \S", line):  # next tool entry
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

    tool_updates: dict[str, str] = {}
    for name, spec in (versions.get("tools") or {}).items():
        if not isinstance(spec, dict):
            continue
        latest, status = _latest_tool_release(spec, now)
        if status == "ok" and latest:
            tool_updates[name] = latest
            print(f"  {name}: {spec.get('version')} → {latest}")
        elif status == "lookup-failed":
            # Say so. A tool we could not reach is not a tool that is current.
            print(f"  {name}: {spec.get('version')} (COULD NOT CHECK — skipped)")

    runtimes = versions.get("runtimes", {})
    for name in _AUTO_UPDATE_SKIP:
        if runtimes.get(name):
            print(f"  {name}: {runtimes[name]} (manual — skipped)")

    if not action_updates and not sr_update and not tool_updates:
        print("\nNo auto-updates available.")
        return 0

    total = len(action_updates) + len(tool_updates) + (1 if sr_update else 0)
    print(f"\n{total} update(s) to apply.")

    original_yaml = _VERSIONS_FILE.read_text(encoding="utf-8")
    # Snapshot the tool pin files too, not just the pipeline YAML: --apply
    # rewrites the mirrored constants (e.g. gitleaks.py) as well, and a revert
    # that skipped them would leave the SSOT and the source diverged - in the
    # very path whose job is to restore safety.
    original_files = {
        str(p): p.read_text(encoding="utf-8")
        for p in {*_find_workflow_files(), *(pin[0] for pin in _tool_pins(versions)[0])}
    }

    yaml_content = original_yaml
    for short_name, spec in action_updates.items():
        yaml_content = _set_action_spec_in_yaml(
            yaml_content, short_name, spec["version"], spec["sha"]
        )
    for tool_name, tool_version in tool_updates.items():
        yaml_content = _set_tool_version_in_yaml(yaml_content, tool_name, tool_version)
    if sr_update:
        yaml_content = re.sub(
            r'(?m)^(  core:\s*")[^"]*(")', rf"\g<1>{sr_update[1]}\g<2>", yaml_content
        )
    _VERSIONS_FILE.write_text(yaml_content, encoding="utf-8", newline="\n")

    def _revert(reason: str) -> None:
        print(f"\n{reason} Reverting all changes...")
        _VERSIONS_FILE.write_text(original_yaml, encoding="utf-8", newline="\n")
        for path_str, content in original_files.items():
            Path(path_str).write_text(content, encoding="utf-8", newline="\n")
        print("Reverted.")

    # try/finally, not just an `if failures`: from here on versions.yaml is
    # ALREADY mutated, so any throw - a corrupt bumped YAML failing to re-parse
    # in _apply, a KeyboardInterrupt mid-validate - would otherwise leave the
    # SSOT bumped, the pipeline half-rewritten, and no revert. The failure path
    # of a "revert on failure" feature must not itself be the one that strands
    # the repo.
    reverted = False
    try:
        print("\nApplying to pipeline files...")
        _apply(_load_versions())

        print("\nValidating locally (YAML parse, SSOT sync, workflow pytest gates)...")
        failures = _validate_locally()

        if failures:
            print(f"\n{len(failures)} local gate(s) failed:")
            for f in failures:
                print(f"  {f}")
            _revert("Local gates failed.")
            reverted = True
            print("Fix the issues and try again.")
            return 1
    except BaseException as exc:  # noqa: BLE001 - re-raised after reverting
        if not reverted:
            _revert(f"Aborted ({type(exc).__name__}: {exc}).")
        raise

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
