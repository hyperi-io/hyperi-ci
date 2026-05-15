# Project:   HyperI CI
# File:      tests/unit/test_commit_validation.py
# Purpose:   Tests for commit message validation
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
from __future__ import annotations

from hyperi_ci.quality.commit_validation import (
    ValidationResult,
    format_rejection,
    format_type_list,
    validate_message,
)

# ---------------------------------------------------------------------------
# Valid messages
# ---------------------------------------------------------------------------


class TestValidMessages:
    def test_fix_prefix(self) -> None:
        result = validate_message("fix: correct null pointer in parser")
        assert result.valid is True
        assert result.error_type == ""

    def test_feat_with_scope_requires_opt_in(self, monkeypatch) -> None:
        """`feat:` is gated behind HYPERCI_ALLOW_FEAT to enforce HyperI policy.

        feat: triggers a MINOR semver bump, but HyperI policy is to use feat:
        rarely (genuinely new user-facing features only). The gate forces
        the operator to opt in deliberately.
        """
        # Without opt-in: rejected. Explicitly clear the env var in case
        # the calling shell has it set (e.g. from `hyperi-ci push --allow-feat`
        # which exports HYPERCI_ALLOW_FEAT=1 to the test phase too).
        monkeypatch.delenv("HYPERCI_ALLOW_FEAT", raising=False)
        result = validate_message("feat(auth): add OAuth2 support")
        assert result.valid is False
        assert result.error_type == "feat_without_opt_in"

        # With opt-in: accepted
        monkeypatch.setenv("HYPERCI_ALLOW_FEAT", "1")
        result = validate_message("feat(auth): add OAuth2 support")
        assert result.valid is True
        assert result.error_type == ""

    def test_sec_alias(self) -> None:
        """sec is an alias for security and must be accepted."""
        result = validate_message("sec: patch SQL injection vulnerability")
        assert result.valid is True

    def test_security_full_name(self) -> None:
        result = validate_message("security: patch SQL injection vulnerability")
        assert result.valid is True

    def test_spike_no_release_type(self) -> None:
        result = validate_message("spike: evaluate new serialisation library")
        assert result.valid is True

    def test_chore(self) -> None:
        result = validate_message("chore: update dependencies")
        assert result.valid is True

    def test_docs(self) -> None:
        result = validate_message("docs: update README")
        assert result.valid is True

    def test_test_type(self) -> None:
        result = validate_message("test: add integration tests for parser")
        assert result.valid is True

    def test_refactor(self) -> None:
        result = validate_message("refactor: extract validation helper")
        assert result.valid is True

    def test_ci_type(self) -> None:
        result = validate_message("ci: add matrix builds for multiple Python versions")
        assert result.valid is True

    def test_infra(self) -> None:
        result = validate_message("infra: provision new k8s nodes")
        assert result.valid is True

    def test_ops(self) -> None:
        result = validate_message("ops: rotate API keys")
        assert result.valid is True

    def test_cleanup(self) -> None:
        result = validate_message("cleanup: remove deprecated handler")
        assert result.valid is True

    def test_data(self) -> None:
        result = validate_message("data: migrate events schema to v3")
        assert result.valid is True

    def test_debt(self) -> None:
        result = validate_message("debt: address legacy timeout handling")
        assert result.valid is True

    def test_design(self) -> None:
        result = validate_message("design: update component interaction diagram")
        assert result.valid is True

    def test_meta(self) -> None:
        result = validate_message("meta: update team contribution process")
        assert result.valid is True

    def test_review(self) -> None:
        result = validate_message("review: audit logging configuration")
        assert result.valid is True

    def test_ui(self) -> None:
        result = validate_message("ui: align button styles with design system")
        assert result.valid is True

    def test_perf(self) -> None:
        result = validate_message("perf: optimise batch processing throughput")
        assert result.valid is True

    def test_hotfix(self) -> None:
        result = validate_message("hotfix: prevent crash on empty payload")
        assert result.valid is True

    def test_description_at_minimum_length(self) -> None:
        """Exactly 3 characters in the description is acceptable."""
        result = validate_message("fix: abc")
        assert result.valid is True

    def test_description_at_maximum_length(self) -> None:
        """Exactly 100 characters in the description is acceptable."""
        description = "a" * 100
        result = validate_message(f"fix: {description}")
        assert result.valid is True


# ---------------------------------------------------------------------------
# Skipped messages (should return valid=True without validation)
# ---------------------------------------------------------------------------


class TestSkippedMessages:
    def test_merge_commit_is_skipped(self) -> None:
        result = validate_message("Merge branch 'main' into feature/something")
        assert result.valid is True

    def test_version_bump_skip_ci_is_skipped(self) -> None:
        result = validate_message("chore: version 1.2.3 [skip ci]")
        assert result.valid is True

    def test_version_bump_dev_skip_ci_is_skipped(self) -> None:
        result = validate_message("chore: version 1.2.3-dev.4 [skip ci]")
        assert result.valid is True


# ---------------------------------------------------------------------------
# Rejections: no_prefix
# ---------------------------------------------------------------------------


class TestNoPrefixRejection:
    def test_plain_sentence_rejected(self) -> None:
        result = validate_message("updated the readme file")
        assert result.valid is False
        assert result.error_type == "no_prefix"

    def test_empty_message_rejected(self) -> None:
        result = validate_message("")
        assert result.valid is False
        assert result.error_type == "no_prefix"

    def test_colon_only_no_type_rejected(self) -> None:
        result = validate_message(": missing type")
        assert result.valid is False
        assert result.error_type == "no_prefix"

    def test_whitespace_only_rejected(self) -> None:
        result = validate_message("   ")
        assert result.valid is False
        assert result.error_type == "no_prefix"


# ---------------------------------------------------------------------------
# Rejections: unknown_type
# ---------------------------------------------------------------------------


class TestUnknownTypeRejection:
    def test_made_up_type_rejected(self) -> None:
        result = validate_message("update: something useful")
        assert result.valid is False
        assert result.error_type == "unknown_type"

    def test_typo_close_to_known_type(self) -> None:
        """A typo near a known type should still be rejected as unknown_type."""
        result = validate_message("fixx: close to fix but not quite")
        assert result.valid is False
        assert result.error_type == "unknown_type"

    def test_capitalised_known_type_rejected(self) -> None:
        """Fix: (capital F) is not a valid type prefix."""
        result = validate_message("Fix: capitalised prefix should fail")
        assert result.valid is False
        # Should be no_prefix or unknown_type — either is acceptable as long
        # as it is invalid.
        assert result.error_type in ("no_prefix", "unknown_type")


# ---------------------------------------------------------------------------
# Rejections: description_too_short
# ---------------------------------------------------------------------------


class TestDescriptionTooShort:
    def test_two_character_description_rejected(self) -> None:
        result = validate_message("fix: ab")
        assert result.valid is False
        assert result.error_type == "description_too_short"

    def test_one_character_description_rejected(self) -> None:
        result = validate_message("fix: a")
        assert result.valid is False
        assert result.error_type == "description_too_short"

    def test_empty_description_after_colon_rejected(self) -> None:
        result = validate_message("fix: ")
        assert result.valid is False
        assert result.error_type in ("description_too_short", "no_prefix")


# ---------------------------------------------------------------------------
# Rejections: description_too_long
# ---------------------------------------------------------------------------


class TestDescriptionTooLong:
    def test_101_character_description_rejected(self) -> None:
        description = "a" * 101
        result = validate_message(f"fix: {description}")
        assert result.valid is False
        assert result.error_type == "description_too_long"

    def test_200_character_description_rejected(self) -> None:
        description = "a" * 200
        result = validate_message(f"fix: {description}")
        assert result.valid is False
        assert result.error_type == "description_too_long"


# ---------------------------------------------------------------------------
# Rejections: uppercase_description
# ---------------------------------------------------------------------------


class TestUppercaseDescription:
    def test_capitalised_first_word_rejected(self) -> None:
        result = validate_message("fix: This starts with a capital letter")
        assert result.valid is False
        assert result.error_type == "uppercase_description"

    def test_all_caps_description_rejected(self) -> None:
        result = validate_message("fix: ALL CAPS DESCRIPTION")
        assert result.valid is False
        assert result.error_type == "uppercase_description"


# ---------------------------------------------------------------------------
# Rejections: ai_attribution
# ---------------------------------------------------------------------------


class TestAiAttribution:
    def test_generated_with_rejected(self) -> None:
        result = validate_message("fix: fix bug\n\nGenerated with Claude Code")
        assert result.valid is False
        assert result.error_type == "ai_attribution"

    def test_co_authored_by_claude_rejected(self) -> None:
        result = validate_message(
            "fix: add feature\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
        )
        assert result.valid is False
        assert result.error_type == "ai_attribution"

    def test_co_authored_by_copilot_rejected(self) -> None:
        result = validate_message(
            "feat: new feature\n\nCo-Authored-By: Copilot <noreply@github.com>"
        )
        assert result.valid is False
        assert result.error_type == "ai_attribution"

    def test_co_authored_by_gemini_rejected(self) -> None:
        result = validate_message(
            "fix: patch\n\nCo-Authored-By: Gemini <noreply@google.com>"
        )
        assert result.valid is False
        assert result.error_type == "ai_attribution"

    def test_assisted_by_cursor_rejected(self) -> None:
        result = validate_message("chore: update config\n\nAssisted by Cursor IDE")
        assert result.valid is False
        assert result.error_type == "ai_attribution"


# ---------------------------------------------------------------------------
# Bump-discipline gates
# ---------------------------------------------------------------------------


class TestFeatGate:
    """`feat:` is gated behind HYPERCI_ALLOW_FEAT.

    Rationale: HyperI policy is `feat:` RARELY (genuinely new user-facing
    feature). LLM/automation tendency is to over-bump by labeling small
    additions as `feat:`. The gate forces a deliberate opt-in decision.
    """

    def test_feat_without_opt_in_rejected(self, monkeypatch) -> None:
        # Same env-leak guard as above — `--allow-feat` exports the env
        # var to the test phase, masking this gate's behaviour.
        monkeypatch.delenv("HYPERCI_ALLOW_FEAT", raising=False)
        result = validate_message("feat: add new endpoint")
        assert result.valid is False
        assert result.error_type == "feat_without_opt_in"
        # Reason mentions both the policy and the opt-in env var
        assert "MINOR bump" in result.reason
        assert "HYPERCI_ALLOW_FEAT" in result.reason

    def test_feat_with_opt_in_accepted(self, monkeypatch) -> None:
        monkeypatch.setenv("HYPERCI_ALLOW_FEAT", "1")
        result = validate_message("feat: add daemon mode")
        assert result.valid is True

    def test_feat_with_opt_in_true_accepted(self, monkeypatch) -> None:
        monkeypatch.setenv("HYPERCI_ALLOW_FEAT", "true")
        result = validate_message("feat: add daemon mode")
        assert result.valid is True

    def test_feat_with_falsy_opt_in_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("HYPERCI_ALLOW_FEAT", "0")
        result = validate_message("feat: add new endpoint")
        assert result.valid is False
        assert result.error_type == "feat_without_opt_in"

    def test_fix_not_gated(self) -> None:
        # Default fix: stays the easy path; no env var required.
        result = validate_message("fix: handle empty config")
        assert result.valid is True


class TestBreakingChangeGate:
    """`BREAKING CHANGE:` in body is gated behind HYPERCI_ALLOW_BREAKING.

    Rationale: semantic-release scans the entire commit body for the
    literal string `BREAKING CHANGE:` and treats it as a footer marker
    triggering a MAJOR bump — even when written as documentation
    reference. The gate forces a deliberate opt-in.
    """

    def test_breaking_change_without_opt_in_rejected(self) -> None:
        msg = (
            "fix: rename helper function\n\n"
            "Note: BREAKING CHANGE: this rename affects downstream code.\n"
        )
        result = validate_message(msg)
        assert result.valid is False
        assert result.error_type == "breaking_change_without_opt_in"
        assert "MAJOR bump" in result.reason

    def test_breaking_change_with_opt_in_accepted(self, monkeypatch) -> None:
        monkeypatch.setenv("HYPERCI_ALLOW_BREAKING", "1")
        msg = (
            "fix: rename helper function\n\n"
            "BREAKING CHANGE: rename affects downstream callers.\n"
        )
        result = validate_message(msg)
        assert result.valid is True

    def test_breaking_change_mid_body_paragraph_gated(self) -> None:
        # Even when the marker appears mid-paragraph (not as a typical
        # footer at the end), conventional-commits-parser scans the
        # whole body and treats it as a major-bump trigger. The gate
        # must catch this case — it's exactly how AI agents have
        # accidentally bumped majors in the past (writing
        # "BREAKING CHANGE:" as a documentation reference).
        msg = (
            "fix: tighten commit guidance\n\n"
            "Note that BREAKING CHANGE: footers must be authored by\n"
            "humans, never auto-generated.\n"
        )
        result = validate_message(msg)
        assert result.valid is False
        assert result.error_type == "breaking_change_without_opt_in"

    def test_uppercase_hyphenated_form_also_gated(self) -> None:
        # `BREAKING-CHANGE:` (with hyphen) is ALSO a conventional-commits
        # major-bump trigger. We block it for the same reason — agents
        # would otherwise write `BREAKING-CHANGE:` as a "documentation
        # reference" and accidentally bump major. The documented escape
        # for documentation references is lowercase or differently-
        # formatted (see test_lowercase_form_not_gated below).
        msg = "fix: rename helper\n\nBREAKING-CHANGE: rename affects callers.\n"
        result = validate_message(msg)
        assert result.valid is False
        assert result.error_type == "breaking_change_without_opt_in"

    def test_lowercase_form_not_gated(self) -> None:
        # semantic-release matches the literal uppercase string only.
        # Free-form body text mentioning 'breaking change' is fine.
        msg = "fix: rephrase docs\n\nThis rephrases the breaking change explanation.\n"
        result = validate_message(msg)
        assert result.valid is True

    def test_descriptive_phrasing_not_gated(self) -> None:
        # Operator-friendly phrases like 'breaking-change footer' or
        # 'breaking change marker' (no colon, no uppercase BREAKING)
        # don't fire. This is the documented escape hatch for
        # commit-body documentation references.
        msg = (
            "fix: tidy commit conventions\n\n"
            "Document the breaking-change footer convention.\n"
            "The breaking change marker is required for major bumps.\n"
        )
        result = validate_message(msg)
        assert result.valid is True


# ---------------------------------------------------------------------------
# format_rejection
# ---------------------------------------------------------------------------


class TestFormatRejection:
    def test_starts_with_computer_says_no(self) -> None:
        result = ValidationResult(
            valid=False, reason="test reason", error_type="no_prefix"
        )
        output = format_rejection(result, "some bad commit message")
        assert output.startswith("Computer says no.")

    def test_includes_original_message(self) -> None:
        original = "this is what I typed"
        result = ValidationResult(
            valid=False, reason="test reason", error_type="no_prefix"
        )
        output = format_rejection(result, original)
        assert original in output

    def test_no_prefix_includes_accepted_prefixes(self) -> None:
        result = ValidationResult(
            valid=False, reason="missing prefix", error_type="no_prefix"
        )
        output = format_rejection(result, "bad message")
        # At minimum, the most common types should appear
        assert "fix:" in output or "feat:" in output or "chore:" in output

    def test_unknown_type_output(self) -> None:
        result = ValidationResult(
            valid=False, reason="unknown type: xyz", error_type="unknown_type"
        )
        output = format_rejection(result, "xyz: do something")
        assert "Computer says no." in output

    def test_description_too_long_output(self) -> None:
        result = ValidationResult(
            valid=False,
            reason="description too long",
            error_type="description_too_long",
        )
        output = format_rejection(result, "fix: " + "a" * 120)
        assert "Computer says no." in output

    def test_ai_attribution_output(self) -> None:
        result = ValidationResult(
            valid=False,
            reason="AI attribution found",
            error_type="ai_attribution",
        )
        output = format_rejection(result, "fix: thing\n\nGenerated with Claude Code")
        assert "Computer says no." in output


# ---------------------------------------------------------------------------
# format_type_list
# ---------------------------------------------------------------------------


class TestFormatTypeList:
    def test_returns_string(self) -> None:
        output = format_type_list()
        assert isinstance(output, str)

    def test_contains_fix(self) -> None:
        output = format_type_list()
        assert "fix" in output

    def test_contains_feat(self) -> None:
        output = format_type_list()
        assert "feat" in output

    def test_contains_chore(self) -> None:
        output = format_type_list()
        assert "chore" in output

    def test_contains_all_release_types(self) -> None:
        output = format_type_list()
        for t in ("fix", "feat", "perf", "hotfix", "sec", "security"):
            assert t in output, f"Expected '{t}' in format_type_list() output"

    def test_not_empty(self) -> None:
        output = format_type_list()
        assert len(output) > 0


# ---------------------------------------------------------------------------
# ValidationResult dataclass
# ---------------------------------------------------------------------------


class TestValidationResult:
    def test_valid_result_has_empty_error_type(self) -> None:
        result = ValidationResult(valid=True, reason="", error_type="")
        assert result.valid is True
        assert result.error_type == ""

    def test_invalid_result_fields(self) -> None:
        result = ValidationResult(
            valid=False, reason="bad prefix", error_type="no_prefix"
        )
        assert result.valid is False
        assert result.reason == "bad prefix"
        assert result.error_type == "no_prefix"
