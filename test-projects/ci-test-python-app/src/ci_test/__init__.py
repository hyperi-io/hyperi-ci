# Project:   CI Test Python Minimal
# File:      src/ci_test/__init__.py
# Purpose:   Minimal Python package for CI pipeline testing
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Minimal Python package for CI pipeline testing."""

from __future__ import annotations

from pathlib import Path

__version__ = (
    (Path(__file__).resolve().parent.parent.parent / "VERSION").read_text().strip()
)


def greet(name: str) -> str:
    """Return a greeting message."""
    return f"Hello, {name}!"


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def main() -> None:
    """CLI entry point."""
    print(f"ci-test-python v{__version__}")
