# Project:   HyperI CI
# File:      tests/unit/test_common.py
# Purpose:   Tests for common utilities
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from hyperi_ci.common import sanitize_ref_name


class TestSanitizeRefName:
    """Sanitize git ref names for use in file paths."""

    def test_branch_with_slash(self) -> None:
        assert sanitize_ref_name("fix/reconcile-release") == "fix-reconcile-release"

    def test_multiple_slashes(self) -> None:
        assert sanitize_ref_name("feat/scope/thing") == "feat-scope-thing"

    def test_no_slash(self) -> None:
        assert sanitize_ref_name("main") == "main"

    def test_tag_version(self) -> None:
        assert sanitize_ref_name("v1.2.3") == "v1.2.3"

    def test_empty_string(self) -> None:
        assert sanitize_ref_name("") == ""
