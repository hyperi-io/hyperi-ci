# Project:   HyperI CI
# File:      src/hyperi_ci/quality/commit_validation.py
# Purpose:   Commit message validation with friendly rejection messages
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Commit message validation with friendly rejection messages."""

import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.commit_range import commits_in_range, event_payload, git_log
from hyperi_ci.common import env_true, error, info, is_ci, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.gh import gh_run
from hyperi_ci.release_rules import load_type_bump
from hyperi_ci.vocabulary import trailer_values

# Accepted message types only: which ones release is hyperi_ci.release_rules' call,
# so `hotfix` / `sec` / `security` are valid but do not bump on their own.
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

# An env var lives only in the committer's shell, so a `feat:` confirmed on a
# branch fails once the squash button rewrites it away (issue #131).
_ALLOW_FEAT_TRAILER = "Allow-Feat"
_TRUTHY_TRAILER = frozenset({"true", "1", "yes"})

_PR_API_TIMEOUT_SECONDS = 30

# GitHub appends ` (#N)` to a squash subject; this covers N up to 9999.
_SQUASH_SUFFIX_RESERVE = 8


@dataclass
class ValidationResult:
    """Result of validating a single commit message."""

    valid: bool
    reason: str
    error_type: str


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


def _feat_confirmed(msg: str) -> bool:
    """Return whether a ``feat:`` is confirmed, by env var or ``Allow-Feat`` trailer.

    Only the trailer survives into the commit that lands.
    """
    if env_true("HYPERCI_ALLOW_FEAT"):
        return True
    return any(
        value.lower() in _TRUTHY_TRAILER
        for value in trailer_values(msg, _ALLOW_FEAT_TRAILER)
    )


def validate_message(msg: str, *, suffix: str = "") -> ValidationResult:
    """Validate a single commit message subject line.

    Args:
        msg: The full commit message.
        suffix: Text a later step appends to the subject, such as the
            ``" (#N)"`` a squash merge adds. It counts towards the length cap.

    Returns:
        ValidationResult with valid=True for skipped messages and valid
        commits, or valid=False with a descriptive error_type.
    """
    if _should_skip(msg):
        return ValidationResult(valid=True, reason="", error_type="")

    # The body is checked too: attribution trailers live there.
    for pattern in _AI_ATTRIBUTION_PATTERNS:
        if re.search(pattern, msg):
            return ValidationResult(
                valid=False,
                reason=f"AI attribution found: matched pattern '{pattern}'",
                error_type="ai_attribution",
            )

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

    if commit_type not in _ALLOWED_TYPES:
        close = difflib.get_close_matches(commit_type, list(_ALLOWED_TYPES), n=3)
        suggestion = f" Did you mean: {', '.join(close)}?" if close else ""
        return ValidationResult(
            valid=False,
            reason=f"unknown commit type '{commit_type}'.{suggestion}",
            error_type="unknown_type",
        )

    if len(description) < _MIN_DESCRIPTION_LENGTH:
        return ValidationResult(
            valid=False,
            reason=(
                f"description is too short ({len(description)} chars, "
                f"minimum {_MIN_DESCRIPTION_LENGTH})"
            ),
            error_type="description_too_short",
        )

    if len(description) + len(suffix) > _MAX_DESCRIPTION_LENGTH:
        limit = _MAX_DESCRIPTION_LENGTH - len(suffix)
        landing = (
            f", once the squash merge appends {suffix.strip()!r}" if suffix else ""
        )
        return ValidationResult(
            valid=False,
            reason=(
                f"description is too long ({len(description)} chars, "
                f"maximum {limit}{landing})"
            ),
            error_type="description_too_long",
        )

    if description and description[0].isupper():
        return ValidationResult(
            valid=False,
            reason=(
                "description must start with a lowercase letter "
                f"(got '{description[0]}')"
            ),
            error_type="uppercase_description",
        )

    # Bump-discipline gates, kept because AI agents default to `feat:` and write
    # `BREAKING CHANGE:` as prose, which semantic-release reads as a real major.
    # An opt-in costs seconds; rolling back a wrong release costs tags and yanks.
    if commit_type == "feat" and not _feat_confirmed(msg):
        return ValidationResult(
            valid=False,
            reason=(
                "`feat:` triggers a MINOR bump. HyperI policy is to use "
                "`feat:` RARELY -- for genuinely new user-facing features. "
                "Adding a CLI flag, config knob, helper, or refinement is "
                "`fix:`, not `feat:`. If this commit IS a genuinely new "
                "feature (not just an improvement), set HYPERCI_ALLOW_FEAT=1 "
                "to confirm: `HYPERCI_ALLOW_FEAT=1 git commit ...`, or write "
                f"`{_ALLOW_FEAT_TRAILER}: true` as a trailer in the commit "
                "body, which survives a squash merge where the env var cannot"
            ),
            error_type="feat_without_opt_in",
        )

    if _has_breaking_change_marker(msg) and not env_true("HYPERCI_ALLOW_BREAKING"):
        return ValidationResult(
            valid=False,
            reason=(
                "Commit body contains `BREAKING CHANGE:` -- this triggers a "
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
    """Return True when ``msg`` contains ``BREAKING CHANGE:`` or ``BREAKING-CHANGE:``.

    Lowercase and colon-less forms pass. The match is unanchored because
    semantic-release fires on the marker anywhere in the body, mid-line included.
    """
    return bool(_BREAKING_CHANGE_RE.search(msg))


def format_type_list() -> str:
    """Return the valid commit types, one per line.

    The ``[release]`` marker comes from :mod:`hyperi_ci.release_rules`.
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
    first_line = (original.splitlines() or [""])[0]
    lines.append(f"  Commit: {first_line!r}")
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


def run(
    config: CIConfig | None = None,
    extra_env: dict[str, str] | None = None,
    *,
    local: bool = False,
) -> int:
    """Validate the commit messages the CI event (or the local range) introduced.

    ``config`` is unused, so the ``check-commits`` command can call ``run()`` bare.

    - ``push``: fatal. This is the landing gate, because run-checks skips the
      quality job on non-publish main pushes.
    - ``pull_request``: branch commits are advisory, since a squash may discard
      them. The line a squash lands is fatal: the PR title, or on a one-commit
      PR under GitHub's default ``COMMIT_OR_PR_TITLE``, that commit's message.
    - ``merge_group``: fatal; the queue's squash commit is what main becomes.

    ``local=True`` runs outside CI over ``origin/main..HEAD``, fatal, as the
    ``hyperi-ci check`` pre-push backstop. Without it, outside CI is a no-op.
    """
    if not local and not is_ci():
        info("Skipping commit message validation (not in CI)")
        return 0

    rc, messages = _validate_range()
    if os.environ.get("GITHUB_EVENT_NAME", "") == "pull_request":
        rc = max(rc, _validate_squash_subject(messages))
    return rc


def _validate_range() -> tuple[int, list[str]]:
    """Validate every commit the event introduced.

    Returns:
        The exit code, and the messages that were validated.
    """
    commits, resolved = commits_in_range()

    if not resolved:
        # An unresolvable range validates HEAD and warns, never passes (issue #52).
        rc, head = git_log(["-1", "HEAD"])
        if rc != 0 or not head:
            warn(
                "Commit validation could not resolve any commit to check "
                "(not a git repo, or empty HEAD). Backstop did NOT run."
            )
            return 0, []
        warn(
            "Commit validation could not resolve the pushed range (shallow "
            "checkout / detached HEAD) - validating HEAD only. The full-range "
            "backstop is DEGRADED; set `fetch-depth: 0` on the quality checkout."
        )
        commits = head
    elif not commits:
        info("No new commits to validate")
        return 0, []

    messages = [full_msg for _hash, full_msg in commits]
    failures: list[tuple[str, str, ValidationResult]] = []

    for commit_hash, full_msg in commits:
        result = validate_message(full_msg)
        if not result.valid:
            failures.append((commit_hash, full_msg, result))

    if failures:
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
                "are enforced. A squash merge lands the PR title instead, "
                "which is checked separately and is not advisory."
            )
            return 0, messages

        error(
            f"{len(failures)} commit(s) failed validation. "
            "Please amend or rebase before merging."
        )
        return 1, messages

    success(f"All {len(commits)} commit(s) passed message validation")
    return 0, messages


def _api_object(path: str) -> dict | None:
    """Fetch one GitHub API object, or None when it cannot be read."""
    try:
        result = gh_run(
            ["api", path],
            check=False,
            timeout=_PR_API_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _current_pr() -> dict | None:
    """Return the pull request being validated, from the API, else the payload.

    The payload is frozen at trigger time and a re-run replays it, so it goes
    stale once the title is edited.
    """
    payload_pr = event_payload().get("pull_request") or {}
    number = payload_pr.get("number")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    has_token = bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))

    if repo and number and has_token:
        live = _api_object(f"repos/{repo}/pulls/{number}")
        if live is not None and isinstance(live.get("title"), str):
            return live
        reason = f"could not read PR #{number} from the GitHub API"
    else:
        reason = "no GH_TOKEN to read the PR from the GitHub API"

    if not isinstance(payload_pr.get("title"), str):
        warn(f"PR title NOT validated: {reason}, and the event payload has no title.")
        return None
    warn(
        f"Validating the PR title from the event payload: {reason}. A re-run "
        "replays the title the run started with, so after editing the title "
        "push a commit to validate the new one."
    )
    return payload_pr


def _commit_count(pr: dict, branch_messages: list[str]) -> int:
    """Count the PR's commits, from the API object, else the validated range.

    The skip patterns drop the synthetic merge commit a PR checkout carries.
    """
    count = pr.get("commits")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return sum(1 for msg in branch_messages if not _should_skip(msg))


def _squash_suffix(pr: dict) -> str:
    """Return the ``" (#N)"`` a squash subject gets, padded when N is unknown."""
    number = pr.get("number")
    if isinstance(number, int) and not isinstance(number, bool):
        return f" (#{number})"
    return " (#" + "N" * (_SQUASH_SUFFIX_RESERVE - 4) + ")"


def _title_result(pr: dict, branch_messages: list[str]) -> ValidationResult:
    """Validate the PR title as the subject a squash merge lands.

    A ``feat:`` title needs the ``Allow-Feat`` trailer in a branch commit: the
    squash body is built from those, never from the PR description.
    """
    result = validate_message(pr["title"], suffix=_squash_suffix(pr))
    if result.error_type != "feat_without_opt_in":
        return result
    if any(_feat_confirmed(msg) for msg in branch_messages):
        return ValidationResult(valid=True, reason="", error_type="")
    body = pr.get("body")
    in_description = isinstance(body, str) and _feat_confirmed(body)
    where = (
        "The PR description carries the trailer, but a squash merge does not "
        "land the description. "
        if in_description
        else ""
    )
    return ValidationResult(
        valid=False,
        reason=(
            f"`feat:` triggers a MINOR bump. {where}Confirm it with "
            f"`{_ALLOW_FEAT_TRAILER}: true` as a trailer in a commit on the "
            "branch: the squash body is built from the branch's commit "
            "messages, so that is the copy that reaches main"
        ),
        error_type="feat_without_opt_in",
    )


def _validate_title_lands(pr: dict, branch_messages: list[str], why: str) -> int:
    """Validate the PR title as the subject a squash merge lands. Fatal."""
    title = pr["title"]
    result = _title_result(pr, branch_messages)
    if result.valid:
        success(f"Squash subject passed validation (PR title; {why}): {title!r}")
        return 0

    error(f"[PR title] {format_rejection(result, title)}")
    error(
        f"The PR title failed validation ({why}). A squash merge makes it the "
        "commit subject on main, where the push run rejects it. Edit the "
        "title, then push a commit or re-run this job."
    )
    return 1


def _validate_one_commit_lands(pr: dict, branch_messages: list[str], why: str) -> int:
    """Validate the message a one-commit PR's squash lands; the title is advisory."""
    head_sha = (pr.get("head") or {}).get("sha")
    rc, commits = (
        git_log(["-1", head_sha]) if isinstance(head_sha, str) and head_sha else (1, [])
    )
    if rc != 0 or not commits:
        warn(
            "Could not read the PR's one commit from its head sha; validating "
            "the PR title as the landing subject instead."
        )
        return _validate_title_lands(pr, branch_messages, why)

    commit_hash, message = commits[0]
    title_result = _title_result(pr, [message])
    if not title_result.valid:
        warn(f"[PR title] {format_rejection(title_result, pr['title'])}")
        warn(
            f"The PR title will not land ({why}): a squash merge lands the "
            "commit's own message, so the title is advisory."
        )

    subject = message.splitlines()[0]
    result = validate_message(message, suffix=_squash_suffix(pr))
    if result.valid:
        success(
            f"Squash subject passed validation (commit {commit_hash[:8]}; "
            f"{why}): {subject!r}"
        )
        return 0

    error(f"[{commit_hash[:8]}] {format_rejection(result, message)}")
    error(
        f"The PR's one commit failed validation ({why}). A squash merge lands "
        "its message on main, where the push run rejects it. Reword the "
        "commit and push, or add a second commit so the PR title lands."
    )
    return 1


def _validate_squash_subject(branch_messages: list[str]) -> int:
    """Validate the line a squash merge of the PR would land on main.

    That is the commit's own message on a one-commit PR, else the title.

    Args:
        branch_messages: Messages of the pull request's branch commits.

    Returns:
        1 when the landing line fails validation, else 0.
    """
    pr = _current_pr()
    if pr is None:
        return 0

    count = _commit_count(pr, branch_messages)
    why = f"{count} commit(s)"
    # Assumes COMMIT_OR_PR_TITLE, GitHub's default, which every hyperi-io repo uses.
    if count == 1:
        return _validate_one_commit_lands(pr, branch_messages, why)
    return _validate_title_lands(pr, branch_messages, why)
