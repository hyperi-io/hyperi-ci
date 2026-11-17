# Project:   HyperI CI
# File:      tests/unit/test_licenses.py
# Purpose:   Tests for the classification -> outbound licence table
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the classification -> outbound licence table."""

import pytest

from hyperi_ci import classification, licenses


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("internal", "LicenseRef-HyperI-Proprietary"),
        ("product", "BUSL-1.1"),
        ("general-oss", "Apache-2.0"),
        ("fork", licenses.INHERIT),
    ],
)
def test_each_classification_ships_under_its_licence(
    category: str, expected: str
) -> None:
    assert licenses.outbound(category)["licences"][0] == expected


def test_every_canonical_classification_has_an_entry() -> None:
    assert set(classification.CANONICAL) <= set(licenses.outbound_table())


def test_an_unknown_classification_has_no_entry() -> None:
    assert licenses.outbound("not-a-category") == {}


def test_own_licences_are_the_ones_migrate_may_replace() -> None:
    own = licenses.own_licences()
    assert {"BUSL-1.1", "LicenseRef-HyperI-Proprietary"} <= own
    assert "Apache-2.0" not in own
