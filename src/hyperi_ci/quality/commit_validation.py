# Project:   HyperI CI
# File:      src/hyperi_ci/quality/commit_validation.py
# Purpose:   Commit message validation with friendly rejection messages
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Commit message validation with friendly rejection messages."""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.release_rules import load_type_bump

# ---------------------------------------------------------------------------
# Commit-type allowlist (message-shape policy)
# ---------------------------------------------------------------------------
#
# The curated set of type prefixes `hyperi-ci check-commit` accepts, each with
# a one-line description, powering the friendly "did you mean" suggestion and a
# consistent house vocabulary. This is a MESSAGE policy and is deliberately
# distinct from the version-bump SSoT: which of these actually SHIP a release
# is decided by hyperi_ci.release_rules (semantic-release's own defaults), NOT
# here. So `hotfix` / `sec` / `security` remain valid messages but no longer
# bump on their own - ship a security patch as `fix(security): ...`.

_ALLOWED_TYPES: dict[str, str] = {
    "feat": "New user-facing feature",
    "fix": "Bug fix or improvement",
    "perf": "Performance optimisation",
    "hotfix": "Critical production fix",
    "sec": "Security fix (alias of security)",
    "security": "Security fix or hardening",
    "docs": "Documentation update",
    "test": "Test coverage or QA",
    "chore": "Maintenance, dependencies, config",
    "ci": "CI/CD configuration",
    "refactor": "Code restructure",
    "style": "Formatting, whitespace",
    "build": "Build system changes",
    "deps": "Dependency updates",
    "revert": "Revert a previous commit",
    "wip": "Work in progress",
    "cleanup": "Remove deprecated code",
    "data": "Data model or schema changes",
    "debt": "Technical debt",
    "design": "Architecture or UX design",
    "infra": "Infrastructure changes",
    "meta": "Process or workflow",
    "ops": "Operational maintenance",
    "review": "Internal review or audit",
    "spike": "Research or proof-of-concept",
    "ui": "Frontend or visual improvements",
}

_AI_ATTRIBUTION_PATTERNS = [
    r"Generated with",
    r"Co-Authored-By:.*?(Claude|Copilot|Cursor|Codex|Gemini|Windsurf)",
    r"Assisted by.*(Claude|Copilot|Cursor|Codex|Gemini|Windsurf)",
]

_MIN_DESCRIPTION_LENGTH = 3
_MAX_DESCRIPTION_LENGTH = 100


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of validating a single commit message."""

    valid: bool
    reason: str
    error_type: str


# ---------------------------------------------------------------------------
# Core validation logic
# ---------------------------------------------------------------------------

_SKIP_PATTERNS = [
    re.compile(r"^Merge "),
    re.compile(r"^chore: version .+ \[skip ci\]$"),
]

_PREFIX_RE = re.compile(r"^([a-z][a-z0-9_-]*)(?:\([^)]*\))?:\s*(.*)", re.DOTALL)


def _should_skip(msg: str) -> bool:
    """Return True if this commit message should be exempt from validation."""
    for pattern in _SKIP_PATTERNS:
        if pattern.search(msg.strip()):
            return True
    return False


def validate_message(msg: str) -> ValidationResult:
    """Validate a single commit message subject line.

    Returns ValidationResult with valid=True for skipped messages and
    valid commits, or valid=False with a descriptive error_type.
    """
    if _should_skip(msg):
        return ValidationResult(valid=True, reason="", error_type="")

    # Check for AI attribution anywhere in the full message (including body)
    for pattern in _AI_ATTRIBUTION_PATTERNS:
        if re.search(pattern, msg):
            return ValidationResult(
                valid=False,
                reason=f"AI attribution found: matched pattern '{pattern}'",
                error_type="ai_attribution",
            )

    # Parse the prefix — only look at the subject line (first line)
    subject = msg.split("\n")[0].strip()
    match = _PREFIX_RE.match(subject)
    if not match:
        return ValidationResult(
            valid=False,
            reason="commit message must start with '<type>: <description>'",
            error_type="no_prefix",
        )

    commit_type = match.group(1)
    description = match.group(2).strip()

    # Validate type
    if commit_type not in _ALLOWED_TYPES:
        close = difflib.get_close_matches(commit_type, list(_ALLOWED_TYPES), n=3)
        suggestion = f" Did you mean: {', '.join(close)}?" if close else ""
        return ValidationResult(
            valid=False,
            reason=f"unknown commit type '{commit_type}'.{suggestion}",
            error_type="unknown_type",
        )

    # Validate description length
    if len(description) < _MIN_DESCRIPTION_LENGTH:
        return ValidationResult(
            valid=False,
            reason=(
                f"description is too short ({len(description)} chars, "
                f"minimum {_MIN_DESCRIPTION_LENGTH})"
            ),
            error_type="description_too_short",
        )

    if len(description) > _MAX_DESCRIPTION_LENGTH:
        return ValidationResult(
            valid=False,
            reason=(
                f"description is too long ({len(description)} chars, "
                f"maximum {_MAX_DESCRIPTION_LENGTH})"
            ),
            error_type="description_too_long",
        )

    # Validate first character is not uppercase
    if description and description[0].isupper():
        return ValidationResult(
            valid=False,
            reason=(
                "description must start with a lowercase letter "
                f"(got '{description[0]}')"
            ),
            error_type="uppercase_description",
        )

    # =========================================================================
    # Bump-discipline gates — DO NOT REMOVE without reading this comment.
    # =========================================================================
    #
    # These two gates exist for ONE reason: AI coding agents (Claude Code,
    # Cursor, Copilot, et al.) repeatedly over-bump semver and produce
    # unintended major/minor releases. The maintainer has had to revert
    # accidental bumps and reset main HISTORY *multiple times across
    # multiple sessions* because:
    #
    #   1. Agents default to `feat:` for any new capability — adding a CLI
    #      flag, a config knob, a helper function, a small new branch in
    #      existing code. HyperI policy is that `feat:` is RARE — only for
    #      genuinely new user-facing features. Agents do not respect that
    #      policy reliably even when it's documented in CLAUDE.md, in the
    #      universal rules file, in the project STATE.md, in per-session
    #      memory files, AND when the user has explicitly told the agent
    #      "don't do this" in prior sessions. Memory-based discipline has
    #      failed at least a dozen times.
    #
    #   2. Agents write `BREAKING CHANGE:` in commit body text as a
    #      *documentation reference* — e.g. "Major bumps require a
    #      BREAKING CHANGE: footer". semantic-release's commit-analyzer
    #      cannot distinguish a documentation reference from an actual
    #      breaking-change declaration; the literal string fires the
    #      major-bump detection regardless of authorial intent. Agents
    #      have triggered accidental v2.0.0, v3.0.0 bumps this way despite
    #      being repeatedly warned.
    #
    # Cost to humans: an extra env-var prefix on the rare commit that IS a
    # genuine feat: or breaking change. ~3 seconds of typing per intentional
    # major/minor bump. This is the price humans now pay for AI agents'
    # inability to follow stated commit-type discipline. The trade is
    # worthwhile because rolling back a semver mistake is FAR more painful
    # — git history rewrite, force-push, deleted tags, sometimes yanked
    # PyPI/crates packages, downstream consumers that pulled the wrong
    # version.
    #
    # If you (a human reading this comment) are tempted to remove these
    # gates because they slow you down: understand that you are NOT the
    # primary failure mode they exist for. AI agents are. Removing them
    # will reintroduce the regression. Find another way to streamline
    # your workflow — e.g. set HYPERCI_ALLOW_FEAT=1 in your shell rc on
    # branches where genuine features are expected.
    #
    # =========================================================================

    if commit_type == "feat" and not _env_bypass("HYPERCI_ALLOW_FEAT"):
        return ValidationResult(
            valid=False,
            reason=(
                "`feat:` triggers a MINOR bump. HyperI policy is to use "
                "`feat:` RARELY — for genuinely new user-facing features. "
                "Adding a CLI flag, config knob, helper, or refinement is "
                "`fix:`, not `feat:`. If this commit IS a genuinely new "
                "feature (not just an improvement), set HYPERCI_ALLOW_FEAT=1 "
                "to confirm: `HYPERCI_ALLOW_FEAT=1 git commit ...`"
            ),
            error_type="feat_without_opt_in",
        )

    if _has_breaking_change_marker(msg) and not _env_bypass("HYPERCI_ALLOW_BREAKING"):
        return ValidationResult(
            valid=False,
            reason=(
                "Commit body contains `BREAKING CHANGE:` — this triggers a "
                "MAJOR bump even when written as documentation reference. "
                "Rephrase as `breaking-change footer` or `breaking change "
                "marker`. If a major bump IS intentional, set "
                "HYPERCI_ALLOW_BREAKING=1 to confirm: "
                "`HYPERCI_ALLOW_BREAKING=1 git commit ...`"
            ),
            error_type="breaking_change_without_opt_in",
        )

    return ValidationResult(valid=True, reason="", error_type="")


_BREAKING_CHANGE_RE = re.compile(r"BREAKING[ \-]CHANGE:")


def _has_breaking_change_marker(msg: str) -> bool:
    """Return True when ``msg`` contains a ``BREAKING CHANGE:`` / ``BREAKING-CHANGE:`` marker.

    Both forms are recognised by conventional-commits-parser as the
    breaking-change footer marker. Lowercase variants and free-form
    text like "breaking change" pass through unblocked, as do other
    hyphenations like "breaking-change footer" used as documentation.

    Match is unanchored deliberately — semantic-release scans for the
    literal string anywhere in the message body, and agents have
    triggered major bumps with the marker mid-line in body text.
    Better to over-block (operator sets HYPERCI_ALLOW_BREAKING=1 once)
    than under-block (accidental major release).
    """
    return bool(_BREAKING_CHANGE_RE.search(msg))


def _env_bypass(name: str) -> bool:
    """Return True when env var ``name`` is set to a truthy value."""
    val = os.environ.get(name, "").strip().lower()
    return val in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_type_list() -> str:
    """Return a formatted string listing all valid commit types.

    The ``[release]`` marker is sourced from :mod:`hyperi_ci.release_rules`
    (semantic-release defaults + any repo ``.releaserc.json`` override), so
    the list stays truthful about what actually ships without carrying its
    own copy of the bump map.
    """
    type_bump = load_type_bump(Path.cwd())
    lines: list[str] = []
    for name, desc in sorted(_ALLOWED_TYPES.items()):
        release_marker = " [release]" if type_bump.get(name, "none") != "none" else ""
        lines.append(f"  {name}:{release_marker} {desc}")
    return "\n".join(lines)


def format_rejection(result: ValidationResult, original: str) -> str:
    """Format a friendly 'Computer says no.' rejection message."""
    lines = ["Computer says no.", ""]
    lines.append(f"  Commit: {original.splitlines()[0]!r}")
    lines.append(f"  Reason: {result.reason}")
    lines.append("")

    if result.error_type == "no_prefix":
        lines.append("  Accepted prefixes include:")
        lines.append("")
        lines.append(format_type_list())
        lines.append("")
        lines.append("  Example: fix: correct null pointer in parser")

    elif result.error_type == "unknown_type":
        lines.append("  Valid types:")
        lines.append("")
        lines.append(format_type_list())

    elif result.error_type == "description_too_short":
        lines.append(
            f"  Keep descriptions between {_MIN_DESCRIPTION_LENGTH} and "
            f"{_MAX_DESCRIPTION_LENGTH} characters."
        )

    elif result.error_type == "description_too_long":
        lines.append(f"  Keep the subject under {_MAX_DESCRIPTION_LENGTH} characters.")
        lines.append("  Move additional context into the commit body.")

    elif result.error_type == "uppercase_description":
        lines.append("  Start the description with a lowercase letter.")
        lines.append("  Example: fix: correct the thing  (not: fix: Correct the thing)")

    elif result.error_type == "ai_attribution":
        lines.append("  Remove AI attribution from the commit message.")
        lines.append("  Lines like 'Co-Authored-By: Claude' or 'Generated with ...'")
        lines.append("  should not appear in committed messages.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CI handler
# ---------------------------------------------------------------------------


_COMMIT_SEPARATOR = "----END----"
_COMMIT_FMT = f"%H%n%s%n%b%n{_COMMIT_SEPARATOR}"


def _parse_git_log(output: str) -> list[tuple[str, str]]:
    commits = []
    for block in output.split(_COMMIT_SEPARATOR):
        block = block.strip()
        if not block:
            continue
        first_newline = block.index("\n")
        commit_hash = block[:first_newline].strip()
        full_msg = block[first_newline:].strip()
        if commit_hash and full_msg:
            commits.append((commit_hash, full_msg))
    return commits


def _git_log(args: list[str]) -> tuple[int, list[tuple[str, str]]]:
    """Run ``git log --pretty=<fmt> <args>``; return ``(returncode, commits)``."""
    result = subprocess.run(
        ["git", "log", f"--pretty={_COMMIT_FMT}", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return result.returncode, []
    return 0, _parse_git_log(result.stdout)


def _is_zero_sha(sha: str) -> bool:
    """Return True for git's all-zeros sentinel SHA (branch creation / no parent)."""
    return len(sha) >= 7 and set(sha) == {"0"}


def _event_payload() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _get_commits_to_validate() -> tuple[list[tuple[str, str]], bool]:
    """Return ``(commits, resolved)`` for the commits this CI run should check.

    ``resolved`` is True when we authoritatively determined the range the
    event introduced (even if it is empty — a legitimate "no new commits").
    It is False when we could NOT resolve the range (shallow checkout,
    detached HEAD, missing ``before`` commit) — the caller MUST then treat
    an empty result as a DEGRADED backstop, not as success (issue #52).

    Resolution, in order of authority:

    1. ``push`` event -> ``before..after`` from the event payload. This is
       the ONLY correct range on a push to a tracked branch: after the push,
       the runner's ``origin/<branch>`` already points at HEAD, so
       ``origin/main..HEAD`` is empty and would silently validate nothing.
       Also catches merge-imported history (the range includes commits a
       merge made newly reachable) — the rustlib v3.0.0 class of bug.
    2. ``pull_request`` event -> ``<base sha>..HEAD``.
    3. Generic fallbacks for local / unknown contexts: ``origin/main..HEAD``
       then a bounded ``HEAD~N..HEAD``.

    A resolved-but-empty range short-circuits (returns ``([], True)``) so we
    don't fall through and mis-resolve against a different range.
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    payload = _event_payload()

    if event == "push":
        before = str(payload.get("before", ""))
        after = str(payload.get("after", "")) or "HEAD"
        # A real prior tip gives the authoritative range.
        if before and not _is_zero_sha(before):
            rc, commits = _git_log([f"{before}..{after}"])
            if rc == 0:
                return commits, True
            # We KNOW new commits exist (before != after) but can't enumerate
            # them - `before` isn't in this shallow clone. Do NOT fall through
            # to origin/main..HEAD: right after a push-to-main that range is
            # EMPTY (origin/main already == HEAD) and would wrongly report "no
            # new commits" - the exact silent no-op of issue #52. Degrade to
            # the HEAD-only backstop with a loud warning instead.
            return [], False
        # before is all-zeros (branch creation): no prior tip to diff from, so
        # fall through to the generic ranges (origin/main..HEAD enumerates what
        # the new branch adds over main).
    elif event == "pull_request":
        base = str((payload.get("pull_request") or {}).get("base", {}).get("sha", ""))
        if not base and os.environ.get("GITHUB_BASE_REF"):
            base = f"origin/{os.environ['GITHUB_BASE_REF']}"
        if base:
            rc, commits = _git_log([f"{base}..HEAD"])
            if rc == 0:
                return commits, True

    for git_range in ("origin/main..HEAD", "HEAD~20..HEAD"):
        rc, commits = _git_log([git_range])
        if rc == 0:
            return commits, True

    return [], False


def run(
    config: CIConfig | None = None,
    extra_env: dict[str, str] | None = None,
    *,
    local: bool = False,
) -> int:
    """Validate commit messages in the CI push/PR range (or the local range).

    ``config`` is unused (kept for call-site symmetry) so the CLI
    ``check-commits`` command can call ``run()`` bare.

    Behaviour by event:

    - ``push`` (what lands on main) -> FATAL: a failing commit returns 1.
      This is the real landing gate - the run-checks gate skips the
      quality job on non-publish main pushes, so this dedicated check is
      where merge-to-main enforcement lives.
    - ``pull_request`` -> ADVISORY: failures are warned, returns 0. The
      branch commits validated on a PR may be discarded by a squash-merge
      (only the squash subject lands) and are never re-validated on the
      merge push, so a PR gets feedback rather than a hard red.

    ``local=True`` runs the same check outside CI (``hyperi-ci check``
    pre-push backstop): with no CI event the range falls back to
    ``origin/main..HEAD`` (the unpushed commits) and is FATAL, so a bad
    message is caught before the push, not after. Without ``local`` the
    check is a no-op outside CI (the commit-msg hook covers authoring).
    """
    if not local and not is_ci():
        info("Skipping commit message validation (not in CI)")
        return 0

    commits, resolved = _get_commits_to_validate()

    if not resolved:
        # Could NOT determine the range the event introduced (shallow
        # checkout / detached HEAD / missing `before` commit). The old code
        # returned success here, silently disarming the CI-side backstop on
        # the standard push path - consistent with rustlib v3.0.0 shipping
        # despite this check existing (issue #52). Never silent-skip: fall
        # back to validating HEAD (the tip commit) and warn loudly that the
        # full range was NOT checked. Use `fetch-depth: 0` on the quality
        # checkout to restore full-range validation.
        rc, head = _git_log(["-1", "HEAD"])
        if rc != 0 or not head:
            warn(
                "Commit validation could not resolve any commit to check "
                "(not a git repo, or empty HEAD). Backstop did NOT run."
            )
            return 0
        warn(
            "Commit validation could not resolve the pushed range (shallow "
            "checkout / detached HEAD) - validating HEAD only. The full-range "
            "backstop is DEGRADED; set `fetch-depth: 0` on the quality checkout."
        )
        commits = head
    elif not commits:
        info("No new commits to validate")
        return 0

    failures: list[tuple[str, str, ValidationResult]] = []

    for commit_hash, full_msg in commits:
        result = validate_message(full_msg)
        if not result.valid:
            failures.append((commit_hash, full_msg, result))

    if failures:
        # Advisory on a PR, fatal on push. See the run() docstring: PR
        # branch commits may be squashed away and are never re-validated
        # on the merge-to-main push, so a PR gets feedback - not a hard
        # red - while the push that lands on main stays enforced.
        advisory = os.environ.get("GITHUB_EVENT_NAME", "") == "pull_request"
        emit = warn if advisory else error
        for commit_hash, full_msg, result in failures:
            short_hash = commit_hash[:8]
            rejection = format_rejection(result, full_msg)
            emit(f"[{short_hash}] {rejection}")

        if advisory:
            warn(
                f"{len(failures)} commit(s) would fail validation on merge to "
                "main. Advisory on a PR - only the commit(s) that LAND on main "
                "are enforced. If you squash-merge, make the squash subject a "
                "valid conventional commit (that single line is what lands)."
            )
            return 0

        error(
            f"{len(failures)} commit(s) failed validation. "
            "Please amend or rebase before merging."
        )
        return 1

    success(f"All {len(commits)} commit(s) passed message validation")
    return 0
