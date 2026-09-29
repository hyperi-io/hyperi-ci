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

# An env var lives only in the committer's shell, so a `feat:` confirmed on a
# branch fails once the squash button rewrites it away (issue #131).
_ALLOW_FEAT_TRAILER = "Allow-Feat"
_TRUTHY_TRAILER = frozenset({"true", "1", "yes"})

_PR_API_TIMEOUT_SECONDS = 30

# Values of a repo's `squash_merge_commit_title`; the second is GitHub's default.
_PR_TITLE = "PR_TITLE"
_COMMIT_OR_PR_TITLE = "COMMIT_OR_PR_TITLE"


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


def _feat_confirmed(msg: str) -> bool:
    """Whether a deliberate ``feat:`` carries its confirmation.

    The trailer rides in the message, so it reaches the commit that lands;
    the env var only ever existed where the commit was authored.
    """
    if env_true("HYPERCI_ALLOW_FEAT"):
        return True
    return any(
        value.lower() in _TRUTHY_TRAILER
        for value in trailer_values(msg, _ALLOW_FEAT_TRAILER)
    )


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

    # Parse the prefix -- only look at the subject line (first line)
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
    # Bump-discipline gates -- DO NOT REMOVE without reading this comment.
    # =========================================================================
    #
    # These two gates exist for ONE reason: AI coding agents (Claude Code,
    # Cursor, Copilot, et al.) repeatedly over-bump semver and produce
    # unintended major/minor releases. The maintainer has had to revert
    # accidental bumps and reset main HISTORY *multiple times across
    # multiple sessions* because:
    #
    #   1. Agents default to `feat:` for any new capability -- adding a CLI
    #      flag, a config knob, a helper function, a small new branch in
    #      existing code. HyperI policy is that `feat:` is RARE -- only for
    #      genuinely new user-facing features. Agents do not respect that
    #      policy reliably even when it's documented in CLAUDE.md, in the
    #      universal rules file, in the project STATE.md, in per-session
    #      memory files, AND when the user has explicitly told the agent
    #      "don't do this" in prior sessions. Memory-based discipline has
    #      failed at least a dozen times.
    #
    #   2. Agents write `BREAKING CHANGE:` in commit body text as a
    #      *documentation reference* -- e.g. "Major bumps require a
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
    # -- git history rewrite, force-push, deleted tags, sometimes yanked
    # PyPI/crates packages, downstream consumers that pulled the wrong
    # version.
    #
    # If you (a human reading this comment) are tempted to remove these
    # gates because they slow you down: understand that you are NOT the
    # primary failure mode they exist for. AI agents are. Removing them
    # will reintroduce the regression. Find another way to streamline
    # your workflow -- e.g. set HYPERCI_ALLOW_FEAT=1 in your shell rc on
    # branches where genuine features are expected.
    #
    # =========================================================================

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
    """Return True when ``msg`` contains a ``BREAKING CHANGE:`` / ``BREAKING-CHANGE:`` marker.

    Both forms are recognised by conventional-commits-parser as the
    breaking-change footer marker. Lowercase variants and free-form
    text like "breaking change" pass through unblocked, as do other
    hyphenations like "breaking-change footer" used as documentation.

    Match is unanchored deliberately -- semantic-release scans for the
    literal string anywhere in the message body, and agents have
    triggered major bumps with the marker mid-line in body text.
    Better to over-block (operator sets HYPERCI_ALLOW_BREAKING=1 once)
    than under-block (accidental major release).
    """
    return bool(_BREAKING_CHANGE_RE.search(msg))


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


# ---------------------------------------------------------------------------
# CI handler
# ---------------------------------------------------------------------------


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
    - ``pull_request`` -> ADVISORY for the branch commits: failures are
      warned, returns 0. They may be discarded by a squash-merge and are
      never re-validated on the merge push, so a PR gets feedback rather
      than a hard red. The line a squash merge would LAND is FATAL, because
      the push run rejects it on main: the PR title, or under GitHub's
      default ``COMMIT_OR_PR_TITLE`` on a one-commit PR, that commit's
      message (the title is then advisory).
    - ``merge_group`` (a merge queue entry) -> FATAL. The queue's squash
      commit is the commit main fast-forwards to, so this is the landing
      gate run BEFORE the landing rather than after it.

    ``local=True`` runs the same check outside CI (``hyperi-ci check``
    pre-push backstop): with no CI event the range falls back to
    ``origin/main..HEAD`` (the unpushed commits) and is FATAL, so a bad
    message is caught before the push, not after. Without ``local`` the
    check is a no-op outside CI (the commit-msg hook covers authoring).
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
        # Could NOT determine the range the event introduced (shallow
        # checkout / detached HEAD / missing `before` commit). The old code
        # returned success here, silently disarming the CI-side backstop on
        # the standard push path - consistent with rustlib v3.0.0 shipping
        # despite this check existing (issue #52). Never silent-skip: fall
        # back to validating HEAD (the tip commit) and warn loudly that the
        # full range was NOT checked. Use `fetch-depth: 0` on the quality
        # checkout to restore full-range validation.
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


def _api_ready() -> str | None:
    """Return the repository to query, or None when the API cannot be used."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    has_token = bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    return repo if repo and has_token else None


def _current_pr() -> dict | None:
    """Return the pull request being validated, read live where possible.

    The payload is frozen when the run is triggered: the default
    ``pull_request`` types omit ``edited``, and a re-run replays the original
    payload, so a payload-only check stays red after the title is fixed.
    """
    payload_pr = event_payload().get("pull_request") or {}
    number = payload_pr.get("number")
    repo = _api_ready()

    if repo and number:
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


def _squash_title_setting() -> str:
    """Return the repo's ``squash_merge_commit_title``, else GitHub's default."""
    repo = _api_ready()
    if repo:
        data = _api_object(f"repos/{repo}")
        setting = data.get("squash_merge_commit_title") if data else None
        if isinstance(setting, str) and setting:
            return setting
        reason = f"could not read squash_merge_commit_title for {repo}"
    else:
        reason = "no GH_TOKEN to read the repo's squash_merge_commit_title"
    warn(f"{reason}; assuming GitHub's default, {_COMMIT_OR_PR_TITLE}.")
    return _COMMIT_OR_PR_TITLE


def _commit_count(pr: dict, branch_messages: list[str]) -> int:
    """Count the PR's commits, from the API object or else the validated range.

    The range on a ``pull_request`` checkout carries GitHub's synthetic
    merge commit, which the skip patterns drop from the count.
    """
    count = pr.get("commits")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return sum(1 for msg in branch_messages if not _should_skip(msg))


def _title_result(pr: dict, branch_messages: list[str]) -> ValidationResult:
    """Validate the PR title.

    A ``feat:`` title is confirmed by an ``Allow-Feat`` trailer in the PR
    description or any branch commit, the two sources GitHub builds the
    squash body from.
    """
    result = validate_message(pr["title"])
    if result.error_type == "feat_without_opt_in":
        body = pr.get("body")
        sources = [body if isinstance(body, str) else "", *branch_messages]
        if any(_feat_confirmed(msg) for msg in sources):
            result = ValidationResult(valid=True, reason="", error_type="")
    return result


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
    """Validate the one commit a squash lands under ``COMMIT_OR_PR_TITLE``.

    The commit's own message becomes the squash commit, so it is fatal and
    the title is advisory.
    """
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
    result = validate_message(message)
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

    GitHub picks it from ``squash_merge_commit_title``: ``PR_TITLE`` lands
    the title, and ``COMMIT_OR_PR_TITLE`` (the default) lands a one-commit
    PR's own commit message and the title otherwise. Any other value is
    treated as ``PR_TITLE``.

    Args:
        branch_messages: Messages of the pull request's branch commits.

    Returns:
        1 when the landing line fails validation, else 0.
    """
    pr = _current_pr()
    if pr is None:
        return 0

    setting = _squash_title_setting()
    if setting == _COMMIT_OR_PR_TITLE:
        count = _commit_count(pr, branch_messages)
        why = f"squash_merge_commit_title={setting}, {count} commit(s)"
        if count == 1:
            return _validate_one_commit_lands(pr, branch_messages, why)
        return _validate_title_lands(pr, branch_messages, why)

    if setting != _PR_TITLE:
        info(
            f"squash_merge_commit_title is {setting!r}, not a known value; "
            "validating the PR title as the landing subject."
        )
    return _validate_title_lands(
        pr, branch_messages, f"squash_merge_commit_title={setting}"
    )
