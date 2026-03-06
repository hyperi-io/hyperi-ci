# Project:   HyperI CI
# File:      src/hyperi_ci/__init__.py
# Purpose:   Package root for hyperi-ci CLI tool
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""HyperI CI/CD CLI tool — multi-language build, test, and publish automation."""

from pathlib import Path

__version__ = (
    (Path(__file__).resolve().parent.parent.parent / "VERSION").read_text().strip()
)
