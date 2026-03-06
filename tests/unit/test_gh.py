# Project:   HyperI CI
# File:      tests/unit/test_gh.py
# Purpose:   Tests for shared GitHub CLI helpers
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from unittest.mock import patch

from hyperi_ci.gh import require_gh


class TestRequireGh:
    """Tests for gh CLI detection."""

    def test_returns_true_when_gh_found(self) -> None:
        with patch("hyperi_ci.gh.shutil.which", return_value="/usr/bin/gh"):
            assert require_gh() is True

    def test_returns_false_when_gh_missing(self) -> None:
        with patch("hyperi_ci.gh.shutil.which", return_value=None):
            assert require_gh() is False
