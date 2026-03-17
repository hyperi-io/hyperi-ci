# Project:   HyperI CI
# File:      tests/unit/test_upgrade.py
# Purpose:   Tests for self-upgrade functionality
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

from hyperi_ci.upgrade import _parse_latest_version


class TestParseLatestVersion:
    """Parse latest stable and pre-release versions from PyPI JSON."""

    SAMPLE_RELEASES = {
        "1.0.0": [{}],
        "1.1.0": [{}],
        "1.1.23": [{}],
        "1.2.0": [{}],
        "1.3.0rc1": [{}],
        "1.3.0.dev4": [{}],
    }

    def test_latest_stable(self) -> None:
        stable, _ = _parse_latest_version(self.SAMPLE_RELEASES)
        assert stable == "1.2.0"

    def test_latest_prerelease(self) -> None:
        _, pre = _parse_latest_version(self.SAMPLE_RELEASES)
        assert pre == "1.3.0rc1"

    def test_no_stable_releases(self) -> None:
        releases = {"1.0.0rc1": [{}], "1.0.0.dev1": [{}]}
        stable, pre = _parse_latest_version(releases)
        assert stable is None
        assert pre == "1.0.0rc1"

    def test_empty_releases(self) -> None:
        stable, pre = _parse_latest_version({})
        assert stable is None
        assert pre is None

    def test_ignores_releases_with_no_files(self) -> None:
        releases = {"1.0.0": [{}], "1.1.0": []}
        stable, _ = _parse_latest_version(releases)
        assert stable == "1.0.0"
