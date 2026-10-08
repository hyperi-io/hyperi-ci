#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/update-versions.py
# Purpose:   Sync workflow files with config/versions.yaml SSOT
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Hold the pipeline's tool and runtime pins to the central versions SSOT.

This is the /deps tool for hyperi-ci. It keeps every marked tool and runtime
pin in step with versions.yaml.
`--check` in CI enforces it. Scans both .github/workflows/ and
.github/actions/. GitHub Actions `uses:` refs are Renovate's, not this
script's. Policy + the Renovate split: docs/dependencies/deps-pinning.md.

Usage:
    uv run scripts/update-versions.py                # default: --check
    uv run scripts/update-versions.py --check        # show drift (dry run)
    uv run scripts/update-versions.py --apply        # rewrite pipeline to SSOT
    uv run scripts/update-versions.py --stable       # dry run of --auto-update
    uv run scripts/update-versions.py --stable --now # ... as of now, soak waived
    uv run scripts/update-versions.py --auto-update  # bump SSOT, validate locally, revert on fail

`--check` proves the MIRRORS match the SSOT; it never asks upstream whether the
SSOT itself is behind. `--stable` is that half, and `--fail-on-drift` turns its
report into an exit code so a schedule can act on it (.github/workflows/
versions-audit.yml). Without one, a pin sits stale while every check stays
green.

`--now` waives the soak for a supervised update. The schedule keeps the
cooldown: the soak is the supply-chain control, not a formality.

`--stable` reports the SOAKED release, matching the `stable` channel in
`hyperi-ci autoupdate`. It is the dry run of `--auto-update`: both go through
`_resolve`, so the report names exactly what the update would write.

Update behaviour:
  - Tools resolve to the newest release that has aged past the 7-day
    cooldown, MAJORS INCLUDED. Every bump goes through a PR, so a breaking
    major surfaces as a red CI run rather than a silent behaviour change.
  - Runtimes (python, node, rust) require explicit update -- never auto-bumped.
  - --auto-update applies the bumps then validates LOCALLY (YAML re-parse,
    SSOT sync check, the pytest workflow gates); reverts on local failure.
    It deliberately does NOT trigger remote CI: the ci-test-* projects
    reference the reusable workflows @main, so a remote run validates main,
    not the unpushed bumps -- and it reverted good bumps on unrelated remote
    failures. Real E2E belongs to the branch-mode rehearsal/sweep (see
    docs/plans/2026-07-branch-mode/PLAN.md decisions 4, 5 and 7).
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml

from hyperi_ci import pin_marker
from hyperi_ci.channel import COOLDOWN_DAYS

_ROOT = Path(__file__).resolve().parent.parent
# Inside the package, so it ships in the wheel and runtime reads the SSOT
# itself (hyperi_ci.versions) instead of a constant copied into source.
_VERSIONS_FILE = _ROOT / "src" / "hyperi_ci" / "config" / "versions.yaml"
_WORKFLOWS_DIR = _ROOT / ".github" / "workflows"
_ACTIONS_DIR = _ROOT / ".github" / "actions"

# Waived by --now for a supervised update. Deliberately a per-run override
# rather than a config value: the soak is the supply-chain control, so skipping
# it is a decision a human takes each time, not a default anyone inherits.
_COOLDOWN_OVERRIDE: int | None = None


def _cooldown(explicit: int | None = None) -> int:
    """Days a release must have soaked before it counts as a candidate."""
    if explicit is not None:
        return explicit
    return COOLDOWN_DAYS if _COOLDOWN_OVERRIDE is None else _COOLDOWN_OVERRIDE


# Tag on problems that --apply CANNOT repair (it has nothing to anchor a rewrite
# to). Callers test for this literal instead of pattern-matching prose, so the
# advice cannot silently rot when a message is reworded.
_UNFIXABLE = "NOT-AUTO-FIXABLE"

# A digest is enforced only if it IS one: a truncated or upper-case value would
# otherwise be mirrored verbatim and fail the install at job time instead of here.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _pin_pattern(key: str) -> re.Pattern[str]:
    """Match the version token on the line following this pin's marker.

    The `# hyperi-ci:pin <key>` convention itself lives in
    src/hyperi_ci/pin_marker.py, because `hyperi-ci deps` reads the same lines
    to DISCOVER marked pins in any repo while this script ENFORCES them against
    config/versions.yaml here. One definition, so the two cannot drift apart.

    `key` is the full dotted path into the SSOT - `tools.gitleaks`,
    `runtimes.node` - so one marker vocabulary covers both sections.
    """
    return pin_marker.pin_pattern(key)


def _runtime_value(spec: object) -> object:
    """The version out of a `runtimes:` entry, which may carry its mirrors too.

    A runtime nothing mirrors is a bare value; one that is copied into files
    GitHub parses is a mapping with the version and a `pin:` list.
    """
    return spec.get("version") if isinstance(spec, dict) else spec


def _load_versions() -> dict[str, Any]:
    """Load the versions SSOT file."""
    # Explicit encoding per the project rule: the default follows the locale,
    # and this file DOES contain non-ASCII (the licence header em-dashes).
    with open(_VERSIONS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _marker_pins(
    versions: dict, section: str = "tools"
) -> tuple[list[tuple[Path, re.Pattern[str], str, str]], list[str]]:
    """Resolve one SSOT section to ([(path, pattern, wanted version, key)], problems).

    Only entries with a `pin:` are returned. A pin exists for a value GitHub
    parses before our code runs - a composite action's `default:`, a workflow
    input's - which is the one place a copy is unavoidable. Python reads the
    SSOT at runtime via :mod:`hyperi_ci.versions`, so a Python-only entry has
    no `pin:`, no copy and nothing here to enforce.

    `pin:` is one path or a list of them: a tool's version is mirrored into a
    single composite action, a runtime's into every workflow that takes it as
    an input default.

    A malformed entry is RETURNED AS A REASON, never merely warned about and
    never reduced to a bare count. Two reasons:
      - warn-and-continue dropped the entry out of every downstream check, so
        renaming a pin file without updating `pin:` left the gate green while
        the pin stopped being enforced;
      - a count cannot tell a caller WHICH failure it was, so --check could not
        tell "run --apply" (fixable drift) apart from "fix this by hand"
        (a broken path), and sent everyone to a command that cannot help.
    """
    out: list[tuple[Path, re.Pattern[str], str, str]] = []
    problems: list[str] = []
    for name, spec in (versions.get(section) or {}).items():
        key = f"{section}.{name}"
        if not isinstance(spec, dict):
            # A runtime nothing mirrors stays a bare value (`rust: stable`),
            # so a scalar there is the normal case. A tool is always a mapping,
            # so a scalar there is a malformed entry.
            if section == "tools":
                problems.append(f"  {key}: not a mapping [{_UNFIXABLE}]")
            continue
        version, pin = spec.get("version"), spec.get("pin")
        if not version:
            problems.append(f"  {key}: needs a `version:` [{_UNFIXABLE}]")
            continue
        if not pin:
            continue
        for rel in [pin] if isinstance(pin, str) else pin:
            path = _ROOT / rel
            if not path.is_file():
                problems.append(
                    f"  {key}: `pin:` file does not exist: {rel} [{_UNFIXABLE}]"
                )
                continue
            out.append((path, _pin_pattern(key), str(version), key))

            # The composite runs before hyperi-ci exists, so the digest is
            # mirrored there too (issue #66). A workflow hands the version to an
            # installer action, which does its own fetching, so it carries no
            # digest.
            digests = spec.get("sha256")
            if digests is None or _ACTIONS_DIR not in path.parents:
                continue
            if not isinstance(digests, dict):
                problems.append(f"  {key}.sha256: not a mapping [{_UNFIXABLE}]")
                continue
            for arch, digest in digests.items():
                digest_key = f"{key}.sha256.{arch}"
                if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                    problems.append(
                        f"  {digest_key}: not a sha256 hex digest [{_UNFIXABLE}]"
                    )
                    continue
                out.append(
                    (
                        path,
                        pin_marker.digest_pin_pattern(digest_key),
                        digest,
                        digest_key,
                    )
                )
    return out, problems


def _all_pins(
    versions: dict,
) -> tuple[list[tuple[Path, re.Pattern[str], str, str]], list[str]]:
    """Every marked pin the SSOT declares, across both sections it can pin."""
    pins, problems = _marker_pins(versions, "tools")
    runtime_pins, runtime_problems = _marker_pins(versions, "runtimes")
    return pins + runtime_pins, problems + runtime_problems


def _pin_replacement(version: str) -> str:
    r"""Build the re.sub replacement that swaps in `version`, keeping the prefix.

    This is a REPLACEMENT, not a pattern: only a backslash is special here, so
    re.escape would be the wrong tool (it would insert a literal `v2\.4\.0`).
    """
    return r"\g<1>" + version.replace("\\", "\\\\")


def _pin_mismatches(versions: dict) -> list[str]:
    """Report marked pins that disagree with the SSOT, or that we can't find.

    A pattern matching NOTHING is reported, not ignored: silently rewriting
    zero lines is how a pin drifts for nine months while the check stays green.
    """
    pins, problems = _all_pins(versions)
    for path, pattern, version, key in pins:
        content = path.read_text(encoding="utf-8")
        rel_path = path.relative_to(_ROOT)
        matches = list(pattern.finditer(content))
        if not matches:
            problems.append(
                f"  {rel_path}: no `# hyperi-ci:pin {key}` marker found - "
                f"{key} is no longer being kept in step [{_UNFIXABLE}]"
            )
            continue
        for match in matches:
            # Report the version TOKEN, not the whole match: the match spans the
            # marker line, so echoing it prints a multi-line mess and points the
            # line number at the marker rather than the pin.
            if match.group(2) != version:
                line_num = content[: match.start(2)].count("\n") + 1
                problems.append(
                    f"  {rel_path}:{line_num}: {key} {match.group(2)} -> {version}"
                )
    return problems


def _find_workflow_files() -> list[Path]:
    """Find every pipeline YAML -- workflows AND composite actions.

    Composite actions under `.github/actions/*/action.yml` carry runtime
    literals too, so a scan of workflows alone would leave those unchecked.
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

    Rejects suffixed tags like `v3.1.0-node20` -- those are backports, not
    the canonical latest, and must never win selection. The patch is optional
    because PyPI does not require one: vulture ships `2.16`, and rejecting it
    would leave that pin unmanaged rather than merely unsorted.
    """
    m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$", tag.strip())
    return (int(m[1]), int(m[2]), int(m[3] or 0)) if m else None


def _select_pinned_release(
    releases: list[dict[str, Any]],
    now: datetime,
    cooldown_days: int | None = None,
) -> dict[str, Any] | None:
    """Pick the highest-semver release that has aged past the cooldown.

    Highest semver, NOT newest-published: GitHub republishes old backports
    (e.g. download-artifact `v3.1.0-node20`) with recent dates, so ordering
    by publish date picks the wrong one. Skips drafts, prereleases,
    non-semver tags, and anything without a `published_at`, since the
    cooldown cannot judge a release with no timestamp. Majors are eligible,
    so a breaking bump is caught by CI on the PR rather than blocked here.
    Returns the chosen release dict or None.
    """
    cutoff = now - timedelta(days=_cooldown(cooldown_days))
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


def _build_replacements(versions: dict) -> list[tuple[re.Pattern, str, str]]:
    """Build regex patterns and replacements from versions config.

    Returns list of (pattern, replacement, description) tuples.
    """
    replacements: list[tuple[re.Pattern, str, str]] = []

    runtimes = versions.get("runtimes", {})

    python_ver = _runtime_value(runtimes.get("python"))
    if python_ver:
        # Only match literal versions, not ${{ template expressions }}
        pattern = re.compile(r"(uv python install )(\d[\d.]*)")
        replacement = rf"\g<1>{python_ver}"
        replacements.append((pattern, replacement, f"Python {python_ver}"))

        # The interpreter the CLI itself runs on. Left to drift, uvx takes the
        # project's Python and silently installs an older hyperi-ci that
        # allowed it (issue #157).
        pattern = re.compile(r"(uvx --python )(\d[\d.]*)")
        replacement = rf"\g<1>{python_ver}"
        replacements.append((pattern, replacement, f"CLI Python {python_ver}"))

    node_ver = _runtime_value(runtimes.get("node"))
    if node_ver:
        # Only match literal versions, not ${{ template expressions }}
        pattern = re.compile(r"(node-version: )(\d[\d.]*)")
        replacement = rf"\g<1>{node_ver}"
        replacements.append((pattern, replacement, f"Node.js {node_ver}"))

    rust_ver = _runtime_value(runtimes.get("rust"))
    if rust_ver:
        pattern = re.compile(r"(rust-toolchain.*\n\s+default:\s*)\S+")
        replacement = rf"\g<1>{rust_ver}"
        replacements.append((pattern, replacement, f"Rust {rust_ver}"))

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
                    print(f"  {rel_path}:{line_num}: {match.group(0)} -> {expected}")
                    mismatches += 1

    tool_problems = _pin_mismatches(versions)
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
    # Keyed off _UNFIXABLE, not off prose: matching substrings of `_marker_pins`
    # messages leaves the branch dead the moment one is reworded, and every
    # malformed entry then gets the "run --apply" advice this comment exists to
    # prevent. A marker in the data beats pattern-matching your own messages.
    if any(_UNFIXABLE in p for p in tool_problems):
        print("  Version drift: run --apply.")
        print("  Anything marked NOT-AUTO-FIXABLE: fix by hand, --apply cannot.")
    else:
        print("  Run --apply to fix.")
    return 1


def _rewrite_to_ssot(versions: dict, *, verb: str) -> tuple[int, int]:
    """Rewrite every runtime literal AND marked pin to match the SSOT.

    Returns (lines_changed, unenforceable) - the second being pins the SSOT
    declares but that could NOT be rewritten (marker gone, `pin:` file missing).
    That count is NOT cosmetic: an unenforceable pin is a pin nobody is holding,
    which is the whole failure this design exists to prevent. Callers must treat
    it as failure, not as a warning they scroll past.

    --apply is the only caller, so the workflow loop and the tool-pin loop
    cannot diverge.
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

    pins, problems = _all_pins(versions)
    unenforceable = len(problems)
    for problem in problems:
        print(f"  error:{problem.lstrip()}")
    for path, pattern, version, key in pins:
        original = path.read_text(encoding="utf-8")
        if not pattern.search(original):
            print(
                f"  error: {path.relative_to(_ROOT)}: no `# hyperi-ci:pin"
                f" {key}` marker found - {key} is not being kept in step"
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
        print("No changes needed -- all files match versions.yaml")
    else:
        print(f"\nApplied {total_changes} change(s)")
    if unenforceable:
        # --apply is the "make it so" verb, so it still rewrites what it can -
        # but it must not exit 0 and imply the SSOT is now honoured.
        _report_unenforceable(unenforceable)
        return 1
    return 0


@dataclass(slots=True)
class _Resolution:
    """One resolve pass over the SSOT.

    Attributes:
        tools: Tool name -> the version --auto-update writes.
        digests: Tool name -> the sha256 per asset key written with that version.
        manual: Tools with a newer soaked release that need a hand bump.
        lookup_failures: Tools whose upstream could not be read.
    """

    tools: dict[str, str] = field(default_factory=dict)
    digests: dict[str, dict[str, str]] = field(default_factory=dict)
    manual: int = 0
    lookup_failures: int = 0

    @property
    def writes(self) -> int:
        """Pins --auto-update would rewrite in versions.yaml."""
        return len(self.tools)


def _release_digests(repo: str, tag: str) -> dict[str, str] | None:
    """Map each asset of one GitHub release to the sha256 GitHub recorded for it.

    Assets with no `digest` are left out: GitHub only records one for uploads
    since mid-2025, and an absent digest is never guessed at. None when the
    release cannot be read.
    """
    release = _gh_json(
        f"/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    )
    if not isinstance(release, dict):
        return None
    out: dict[str, str] = {}
    for asset in release.get("assets") or []:
        algo, _, digest = str(asset.get("digest") or "").partition(":")
        if algo == "sha256" and _SHA256_RE.fullmatch(digest):
            out[str(asset.get("name"))] = digest
    return out


def _bumped_digests(spec: dict, new_version: str) -> tuple[dict[str, str] | None, str]:
    """Return the new release's sha256 for every pinned asset key, or why not.

    Each key's asset is found by its PINNED digest in the current release, so no
    asset-name template is restated here: the version in that name is swapped
    for the new one and the new release must carry a digest for it. Any key that
    cannot be carried across leaves the whole tool to a hand bump.
    """
    repo, prefix = str(spec.get("repo") or ""), str(spec.get("tag_prefix") or "")
    cur_version = str(spec.get("version"))
    if not repo:
        return None, "no `repo:` to read digests from"
    current = _release_digests(repo, prefix + cur_version)
    bumped = _release_digests(repo, prefix + new_version)
    if current is None or bumped is None:
        return None, "release lookup failed"
    name_by_digest = {digest: name for name, digest in current.items()}
    old_bare, new_bare = cur_version.removeprefix("v"), new_version.removeprefix("v")
    out: dict[str, str] = {}
    for key, pinned in (spec.get("sha256") or {}).items():
        asset = name_by_digest.get(str(pinned).lower())
        if asset is None:
            return None, f"no {cur_version} release asset has the pinned {key} digest"
        new_asset = asset.replace(old_bare, new_bare)
        if new_asset not in bumped:
            return None, f"{new_version} carries no digest for {new_asset}"
        out[str(key)] = bumped[new_asset]
    return out, ""


def _resolve(versions: dict, now: datetime) -> _Resolution:
    """Resolve every pin to its newest soaked release, printing a line per pin.

    The only resolution path. --stable prints it and writes nothing;
    --auto-update writes exactly what it returns. Runtimes are listed but never
    resolved: a runtime major is a decision, not a dependency refresh.
    """
    res = _Resolution()
    window = _cooldown()
    print(
        "Resolving releases "
        + (
            "(--now: cooldown WAIVED, taking releases as of now)"
            if window == 0
            else f"(>= {window}-day cooldown)"
        )
        + "...\n"
    )

    for name, spec in (versions.get("tools") or {}).items():
        if not isinstance(spec, dict):
            continue
        cur_version = spec.get("version")
        source = spec.get("repo") or spec.get("pypi") or spec.get("npm")
        if not source:
            print(
                f"  {name}: {cur_version} (no `repo:`/`pypi:`/`npm:` -- cannot check)"
            )
            continue
        latest, status = _latest_tool_release(spec, now)
        # Name the TOOL, not the repo: rustsec/rustsec hosts four pinned crates,
        # so "rustsec/rustsec: v0.22.2" is ambiguous.
        label = (
            f"{name} ({source})" if str(spec.get("tag_prefix") or "") else str(source)
        )
        if status == "lookup-failed":
            # Never render a failed lookup as "up to date" - that is a silent
            # skip wearing a green hat.
            print(f"  {label}: {cur_version} (COULD NOT CHECK -- treat as unknown)")
            res.lookup_failures += 1
        elif status == "no-candidate":
            print(f"  {label}: {cur_version} (nothing aged past cooldown)")
        elif status != "ok" or not latest:
            print(f"  {label}: {cur_version} (up to date)")
        elif spec.get("lockfile"):
            # The bump has to land with a relock of the lock's integrity hashes.
            print(
                f"  {label}: {cur_version} -> {latest} (lock-pinned -- run"
                " scripts/relock-node-tools.py --auto-update)"
            )
            res.manual += 1
        elif spec.get("sha256"):
            # A stale digest fails the install closed, so the digest moves with
            # the version or the version does not move.
            digests, why = _bumped_digests(spec, latest)
            if digests is None:
                print(f"  {label}: {cur_version} -> {latest} ({why} -- bump by hand)")
                res.manual += 1
            else:
                print(f"  {label}: {cur_version} -> {latest} (sha256 from the release)")
                res.tools[name] = latest
                res.digests[name] = digests
        else:
            print(f"  {label}: {cur_version} -> {latest}")
            res.tools[name] = latest

    print()
    for name, spec in (versions.get("runtimes") or {}).items():
        print(f"  {name}: {_runtime_value(spec)} (manual -- check release notes)")
    return res


def _stable(versions: dict, *, fail_on_drift: bool = False) -> int:
    """Print what --auto-update would write, and write nothing.

    Soaked, not newest: a release inside the cooldown is reported as held, so
    this answers "what may we pin to now".
    """
    res = _resolve(versions, datetime.now(UTC))
    _report_watchlist(versions)

    if res.writes:
        print(f"\n{res.writes} update(s) --auto-update would write.")
    if res.manual:
        print(f"\n{res.manual} update(s) need a hand bump (marked above).")
    if not (res.writes or res.manual or res.lookup_failures):
        print("\nAll versions up to date.")
    if res.lookup_failures:
        # "All up to date" would be a lie when we could not reach upstream.
        print(
            f"\n{res.lookup_failures} tool(s) COULD NOT BE CHECKED (API error / rate"
            " limit?) -- their status is unknown, not current. Re-run before"
            " trusting this report."
        )
    # A scheduled caller needs a signal, but a human running this wants the
    # report without a non-zero exit, so the failure is opt-in.
    if fail_on_drift and (res.writes or res.manual or res.lookup_failures):
        return 1
    return 0


def _validate_locally() -> list[str]:
    """Validate the applied bumps with LOCAL gates. Returns failure messages.

    Three gates, cheapest first, all offline:
      1. every pipeline YAML still parses,
      2. files match the SSOT (--check clean -- a bad regex rewrite shows
         here as drift or a mangled line),
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


def _tool_releases(spec: dict, releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Narrow a repo's releases to THIS tool's, with tags normalised to semver.

    A monorepo (rustsec/rustsec) tags every crate as `<crate>/vX.Y.Z`, so an
    unfiltered scan mixes crates together, and `_parse_semver` rejects the
    prefixed tag outright - the tool would look permanently up to date while
    actually being unmanaged. `tag_prefix` selects the right crate and strips
    the prefix so the usual semver + cooldown logic applies unchanged.

    `major:` keeps only that major's releases, for a tool held by a known
    incompatibility (versions.yaml says when that applies).
    """
    prefix = str(spec.get("tag_prefix") or "")
    major = spec.get("major")
    out: list[dict[str, Any]] = []
    for rel in releases:
        tag = str(rel.get("tag_name") or "")
        if not tag.startswith(prefix):
            continue
        tag = tag[len(prefix) :]
        if major is not None and (ver := _parse_semver(tag)) and ver[0] != int(major):
            continue
        out.append({**rel, "tag_name": tag})
    return out


def _tag_releases(repo: str, now: datetime) -> list[dict[str, Any]] | None:
    """Tags as pseudo-releases, for a repo that publishes no GitHub Releases.

    golang/vuln stopped cutting releases in 2025-01, so the releases API
    answers v1.1.4 forever and --stable read the pin as current across four
    minors. A tag's date is the date of the commit it points at, and the walk
    stops at the first tag past the cooldown, so the normal case costs one
    extra call rather than one per tag.
    """
    tags = _gh_json(f"/repos/{repo}/tags?per_page=100")
    if not isinstance(tags, list):
        return None
    parsed = [
        (ver, str(tag.get("name")))
        for tag in cast("list[dict[str, Any]]", tags)
        if (ver := _parse_semver(str(tag.get("name") or "")))
    ]
    cutoff = now - timedelta(days=_cooldown())
    out: list[dict[str, Any]] = []
    for _ver, name in sorted(parsed, reverse=True):
        commit = _gh_json(f"/repos/{repo}/commits/{name}")
        if not isinstance(commit, dict):
            continue
        date = commit.get("commit", {}).get("committer", {}).get("date")
        if not date:
            continue
        out.append({"tag_name": name, "published_at": date})
        if datetime.fromisoformat(date.replace("Z", "+00:00")) <= cutoff:
            break
    return out


def _pypi_releases(package: str) -> list[dict[str, Any]] | None:
    """PyPI versions as pseudo-releases, dated by each version's upload time.

    A tool resolved by uvx has no GitHub release to read, and PyPI's own JSON
    carries the two facts the cooldown needs. Yanked files are dropped, because
    a yanked release is one upstream has withdrawn.
    """
    # A PyPI name cannot carry a scheme or a slash, so the URL stays on pypi.org.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", package):
        return None
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            f"https://pypi.org/pypi/{package}/json", timeout=15
        ) as response:
            data = json.load(response)
    except (OSError, json.JSONDecodeError):
        return None
    out: list[dict[str, Any]] = []
    for version, files in (data.get("releases") or {}).items():
        live = [f for f in files if not f.get("yanked")]
        if live:
            out.append(
                {
                    "tag_name": version,
                    "published_at": min(f["upload_time_iso_8601"] for f in live),
                }
            )
    return out


def _npm_releases(package: str) -> list[dict[str, Any]] | None:
    """Return npm versions as pseudo-releases, dated by the registry's publish time.

    Deprecated versions are dropped, the npm equivalent of a PyPI yank.
    """
    # An npm name is `[@scope/]name`, so the URL stays on registry.npmjs.org.
    if not re.fullmatch(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", package):
        return None
    try:
        with urllib.request.urlopen(  # noqa: S310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            f"https://registry.npmjs.org/{package}", timeout=15
        ) as response:
            data = json.load(response)
    except (OSError, json.JSONDecodeError):
        return None
    times = data.get("time") or {}
    out: list[dict[str, Any]] = []
    for version, meta in (data.get("versions") or {}).items():
        if meta.get("deprecated") or version not in times:
            continue
        out.append({"tag_name": version, "published_at": times[version]})
    return out


def _report_watchlist(versions: dict) -> None:
    """Print `watch:` - upstream capabilities we want but that are not ready.

    Surfaced on every --stable run ON PURPOSE. A "revisit this when upstream
    stabilises" decision that lives only in a code comment is a decision nobody
    revisits; printing it at the moment someone is already updating deps is the
    cheapest place to make it resurface.
    """
    watch = versions.get("watch") or {}
    if not watch:
        return
    print("\nWatchlist -- recheck these while you are updating deps:")
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
    """Newest release of a `tools:` entry, past cooldown, across every major.

    Returns (tag_or_None, status) where status is one of `ok` (tag is a real
    upgrade), `current`, `no-candidate` (no release has aged past the
    cooldown), or `lookup-failed`.

    The status is NOT decoration. Collapsing all of these into a bare None made
    the report render an API failure as "(up to date)" - so a rate-limited `gh`
    reported every tool green, which is the same silent-skip shape as a pin that
    nobody enforces.
    """
    cur_version = spec.get("version")
    repo, pypi, npm = spec.get("repo"), spec.get("pypi"), spec.get("npm")
    if not cur_version or not (repo or pypi or npm):
        return None, "lookup-failed"
    if pypi:
        releases = _pypi_releases(str(pypi))
    elif npm:
        releases = _npm_releases(str(npm))
    elif spec.get("release_source") == "tags":
        releases = _tag_releases(str(repo), now)
    else:
        releases = _gh_json(f"/repos/{repo}/releases?per_page=100")
    if not isinstance(releases, list):
        return None, "lookup-failed"
    releases = _tool_releases(spec, cast("list[dict[str, Any]]", releases))
    cur = _parse_semver(str(cur_version))
    # No compatibility clamp. The cooldown still gates freshness, and a tool
    # whose flags changed across a major shows up as a red quality stage on a
    # PR. Clamping meant the estate quietly sat on an old major until someone
    # made the manual edit, which nobody did.
    best = _select_pinned_release(releases, now)
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


def _set_tool_version_in_yaml(
    text: str, name: str, version: str, digests: dict[str, str] | None = None
) -> str:
    """Rewrite one tool's `version:` line, and its `sha256:` keys if given.

    Block-scoped and anchored to the `tools:` section, because a `watch:` or
    `runtimes:` entry can share a tool's short name. Edits lines directly,
    because yaml.safe_dump would strip every comment in the file.
    """
    digests = digests or {}
    out: list[str] = []
    in_tools = False
    in_block = False
    in_sha = False
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
                if re.match(r"^ {0,4}\S", line):  # any key at tool depth ends sha256:
                    in_sha = False
                if re.match(r"^    version:\s", line):
                    # Quoted: a two-component version (vulture 2.16) reads back
                    # as a YAML float, so 2.20 would return as "2.2".
                    out.append(f'    version: "{version}"\n')
                    continue
                if re.match(r"^    sha256:\s*$", line):
                    in_sha = True
                elif in_sha and (m := re.match(r"^      ([\w-]+):\s*\S+\s*$", line)):
                    if m[1] in digests:
                        out.append(f"      {m[1]}: {digests[m[1]]}\n")
                        continue
                if re.match(r"^  \S", line):  # next tool entry
                    in_block = False
        out.append(line)
    return "".join(out)


def _unwritten(text: str, res: _Resolution) -> list[str]:
    """Name every resolved version or digest the rewritten SSOT does not carry.

    The line editor matches by indentation, so a reshaped entry would otherwise
    pass with its version bumped and its old digest kept, which fails the
    install closed only at job time.
    """
    tools = (yaml.safe_load(text) or {}).get("tools") or {}
    missing: list[str] = []
    for name, version in res.tools.items():
        spec = tools.get(name) or {}
        if str(spec.get("version")) != version:
            missing.append(f"SSOT write: tools.{name}.version is not {version}")
        for key, digest in res.digests.get(name, {}).items():
            if (spec.get("sha256") or {}).get(key) != digest:
                missing.append(f"SSOT write: tools.{name}.sha256.{key} was not written")
    return missing


def _auto_update(versions: dict) -> int:
    """Auto-update tools, validate locally, revert on fail.

    Tools resolve to the newest release past the 7-day cooldown, majors
    included. Runtimes never auto-bump. Validation is LOCAL (see
    _validate_locally) -- remote E2E is the branch-mode rehearsal's job, not
    this script's, so a major that breaks at RUNTIME is caught by CI on the
    PR, not here.
    """
    res = _resolve(versions, datetime.now(UTC))

    if not res.writes:
        print("\nNo auto-updates available.")
        return 0

    print(f"\n{res.writes} update(s) to apply.")

    original_yaml = _VERSIONS_FILE.read_text(encoding="utf-8")
    # Snapshot the tool pin files too, not just the pipeline YAML: --apply
    # rewrites the marked pins in composite actions as well, and a revert that
    # skipped them would leave the SSOT and the pins diverged.
    original_files = {
        str(p): p.read_text(encoding="utf-8")
        for p in {*_find_workflow_files(), *(pin[0] for pin in _all_pins(versions)[0])}
    }

    yaml_content = original_yaml
    for tool_name, tool_version in res.tools.items():
        yaml_content = _set_tool_version_in_yaml(
            yaml_content, tool_name, tool_version, res.digests.get(tool_name)
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
        failures = _unwritten(yaml_content, res) or _validate_locally()

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
        "--stable",
        action="store_true",
        help="Print what --auto-update would write, and write nothing",
    )
    group.add_argument(
        "--auto-update",
        action="store_true",
        help="Update non-runtime versions, validate locally, revert on fail",
    )
    # Not in the mutually-exclusive group: it MODIFIES --stable rather than
    # replacing it.
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="With --stable, exit 1 when a pin is behind or could not be checked",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Waive the soak: take releases as of now (supervised updates only)",
    )
    args = parser.parse_args()

    if args.now:
        global _COOLDOWN_OVERRIDE
        _COOLDOWN_OVERRIDE = 0

    versions = _load_versions()

    if args.auto_update:
        return _auto_update(versions)
    if args.stable:
        return _stable(versions, fail_on_drift=args.fail_on_drift)
    if args.apply:
        return _apply(versions)
    return _check(versions)


if __name__ == "__main__":
    sys.exit(main())
