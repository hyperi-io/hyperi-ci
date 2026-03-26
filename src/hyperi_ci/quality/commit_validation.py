# Project:   HyperI CI
# File:      src/hyperi_ci/quality/commit_validation.py
# Purpose:   Commit message validation with friendly rejection messages
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from hyperi_ci.common import error, info, is_ci, success
from hyperi_ci.config import CIConfig

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_FALLBACK_TYPES: dict[str, dict] = {
    "feat": {"release": True, "description": "New user-facing feature"},
    "fix": {"release": True, "description": "Bug fix or improvement"},
    "perf": {"release": True, "description": "Performance optimisation"},
    "hotfix": {"release": True, "description": "Critical production fix"},
    "sec": {"release": True, "alias_for": "security", "description": "Security fix"},
    "security": {"release": True, "description": "Security fix or hardening"},
    "docs": {"release": False, "description": "Documentation update"},
    "test": {"release": False, "description": "Test coverage or QA"},
    "chore": {"release": False, "description": "Maintenance, dependencies, config"},
    "ci": {"release": False, "description": "CI/CD configuration"},
    "refactor": {"release": False, "description": "Code restructure"},
    "style": {"release": False, "description": "Formatting, whitespace"},
    "build": {"release": False, "description": "Build system changes"},
    "deps": {"release": False, "description": "Dependency updates"},
    "revert": {"release": False, "description": "Revert a previous commit"},
    "wip": {"release": False, "description": "Work in progress"},
    "cleanup": {"release": False, "description": "Remove deprecated code"},
    "data": {"release": False, "description": "Data model or schema changes"},
    "debt": {"release": False, "description": "Technical debt"},
    "design": {"release": False, "description": "Architecture or UX design"},
    "infra": {"release": False, "description": "Infrastructure changes"},
    "meta": {"release": False, "description": "Process or workflow"},
    "ops": {"release": False, "description": "Operational maintenance"},
    "review": {"release": False, "description": "Internal review or audit"},
    "spike": {"release": False, "description": "Research or proof-of-concept"},
    "ui": {"release": False, "description": "Frontend or visual improvements"},
}

_AI_ATTRIBUTION_PATTERNS = [
    r"Generated with",
    r"Co-Authored-By:.*?(Claude|Copilot|Cursor|Codex|Gemini|Windsurf)",
    r"Assisted by.*(Claude|Copilot|Cursor|Codex|Gemini|Windsurf)",
]

_MIN_DESCRIPTION_LENGTH = 3
_MAX_DESCRIPTION_LENGTH = 100

_commit_types: dict[str, dict] | None = None
_ai_patterns: list[str] | None = None
_min_len: int = _MIN_DESCRIPTION_LENGTH
_max_len: int = _MAX_DESCRIPTION_LENGTH


def _find_config_root() -> Path:
    """Find the project root by locating the config directory."""
    # Navigate up from this file: quality/ -> hyperi_ci/ -> src/ -> project root
    return Path(__file__).resolve().parents[3]


def _load_config() -> None:
    global _commit_types, _ai_patterns, _min_len, _max_len

    config_path = _find_config_root() / "config" / "commit-types.yaml"
    if config_path.exists():
        try:
            data = yaml.safe_load(config_path.read_text())
            _commit_types = data.get("types", _FALLBACK_TYPES)
            _ai_patterns = data.get("ai_attribution_patterns", _AI_ATTRIBUTION_PATTERNS)
            limits = data.get("description_length", {})
            _min_len = limits.get("min", _MIN_DESCRIPTION_LENGTH)
            _max_len = limits.get("max", _MAX_DESCRIPTION_LENGTH)
        except Exception:
            _commit_types = _FALLBACK_TYPES
            _ai_patterns = _AI_ATTRIBUTION_PATTERNS
    else:
        _commit_types = _FALLBACK_TYPES
        _ai_patterns = _AI_ATTRIBUTION_PATTERNS


def _get_commit_types() -> dict[str, dict]:
    if _commit_types is None:
        _load_config()
    assert _commit_types is not None
    return _commit_types


def _get_ai_patterns() -> list[str]:
    if _ai_patterns is None:
        _load_config()
    assert _ai_patterns is not None
    return _ai_patterns


def _get_limits() -> tuple[int, int]:
    if _commit_types is None:
        _load_config()
    return _min_len, _max_len


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
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
    for pattern in _get_ai_patterns():
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
    known_types = _get_commit_types()
    if commit_type not in known_types:
        close = difflib.get_close_matches(commit_type, list(known_types.keys()), n=3)
        suggestion = f" Did you mean: {', '.join(close)}?" if close else ""
        return ValidationResult(
            valid=False,
            reason=f"unknown commit type '{commit_type}'.{suggestion}",
            error_type="unknown_type",
        )

    # Validate description length
    min_len, max_len = _get_limits()
    if len(description) < min_len:
        return ValidationResult(
            valid=False,
            reason=(
                f"description is too short ({len(description)} chars, "
                f"minimum {min_len})"
            ),
            error_type="description_too_short",
        )

    if len(description) > max_len:
        return ValidationResult(
            valid=False,
            reason=(
                f"description is too long ({len(description)} chars, maximum {max_len})"
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

    return ValidationResult(valid=True, reason="", error_type="")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_type_list() -> str:
    """Return a formatted string listing all valid commit types."""
    types = _get_commit_types()
    lines: list[str] = []
    for name, meta in sorted(types.items()):
        desc = meta.get("description", "")
        release_marker = " [release]" if meta.get("release") else ""
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
        _, max_len = _get_limits()
        lines.append(f"  Keep descriptions between 3 and {max_len} characters.")

    elif result.error_type == "description_too_long":
        _, max_len = _get_limits()
        lines.append(f"  Keep the subject under {max_len} characters.")
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


def _get_commits_to_validate() -> list[tuple[str, str]]:
    """Return list of (hash, full_message) for commits to validate.

    Tries origin/main..HEAD first; falls back to HEAD~10..HEAD for
    shallow clones or detached-HEAD builds.
    """
    separator = "----END----"
    fmt = f"%H%n%s%n%b%n{separator}"

    def _parse(output: str) -> list[tuple[str, str]]:
        commits = []
        for block in output.split(separator):
            block = block.strip()
            if not block:
                continue
            first_newline = block.index("\n")
            commit_hash = block[:first_newline].strip()
            full_msg = block[first_newline:].strip()
            if commit_hash and full_msg:
                commits.append((commit_hash, full_msg))
        return commits

    for git_range in ("origin/main..HEAD", "HEAD~10..HEAD"):
        try:
            result = subprocess.run(
                ["git", "log", f"--pretty={fmt}", git_range],
                capture_output=True,
                text=True,
                check=True,
            )
            commits = _parse(result.stdout)
            if commits:
                return commits
        except subprocess.CalledProcessError:
            continue

    return []


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Validate commit messages in the current branch.

    Only runs inside CI (is_ci() guard). Returns 0 on success, 1 on failure.
    """
    if not is_ci():
        info("Skipping commit message validation (not in CI)")
        return 0

    commits = _get_commits_to_validate()
    if not commits:
        info("No commits to validate")
        return 0

    failures: list[tuple[str, str, ValidationResult]] = []

    for commit_hash, full_msg in commits:
        result = validate_message(full_msg)
        if not result.valid:
            failures.append((commit_hash, full_msg, result))

    if failures:
        for commit_hash, full_msg, result in failures:
            short_hash = commit_hash[:8]
            rejection = format_rejection(result, full_msg)
            error(f"[{short_hash}] {rejection}")

        error(
            f"{len(failures)} commit(s) failed validation. "
            "Please amend or rebase before merging."
        )
        return 1

    success(f"All {len(commits)} commit(s) passed message validation")
    return 0
