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

    def test_feat_with_scope(self) -> None:
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
