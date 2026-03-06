# Project:   CI Test Python Minimal
# File:      tests/unit/test_core.py
# Purpose:   Unit tests for core module
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from ci_test import add, greet


class TestGreet:
    """Tests for greet function."""

    def test_greet_returns_message(self) -> None:
        assert greet("World") == "Hello, World!"

    def test_greet_with_empty_string(self) -> None:
        assert greet("") == "Hello, !"


class TestAdd:
    """Tests for add function."""

    def test_add_positive_numbers(self) -> None:
        assert add(2, 3) == 5

    def test_add_negative_numbers(self) -> None:
        assert add(-1, -2) == -3

    def test_add_zero(self) -> None:
        assert add(0, 0) == 0

    def test_add_mixed(self) -> None:
        assert add(-5, 10) == 5
