# Project:   HyperI CI
# File:      tests/unit/test_licenses.py
# Purpose:   Tests for the licence registry and allow policy
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the licence registry and allow policy."""

from __future__ import annotations

from typing import Any

from hyperi_ci import licenses


def test_default_allowed_is_the_three() -> None:
    assert licenses.allowed_licenses() == {"BUSL-1.1", "Apache-2.0", "MIT"}


def test_extra_extends_allowed_and_keeps_defaults() -> None:
    allowed = licenses.allowed_licenses(["MPL-2.0", " GPL-3.0 "])
    assert "MPL-2.0" in allowed
    assert "GPL-3.0" in allowed  # whitespace stripped
    assert {"BUSL-1.1", "Apache-2.0", "MIT"} <= allowed


def test_is_allowed_defaults_and_override() -> None:
    assert licenses.is_allowed("Apache-2.0")
    assert licenses.is_allowed("MIT")
    assert licenses.is_allowed("BUSL-1.1")
    assert not licenses.is_allowed("GPL-3.0")
    assert licenses.is_allowed("GPL-3.0", ["GPL-3.0"])


def test_is_recognised_covers_common_and_rejects_junk() -> None:
    for lic in ("Apache-2.0", "MIT", "BUSL-1.1", "MPL-2.0", "BSD-3-Clause", "GPL-3.0"):
        assert licenses.is_recognised(lic), lic
    assert not licenses.is_recognised("NOT-A-LICENCE")
    assert not licenses.is_recognised("")


def test_allowed_ignores_non_string_extra() -> None:
    junk: Any = [None, 5, "MIT"]
    assert licenses.allowed_licenses(junk) == {"BUSL-1.1", "Apache-2.0", "MIT"}
