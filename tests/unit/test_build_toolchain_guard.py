# Project:   HyperI CI
# File:      tests/unit/test_build_toolchain_guard.py
# Purpose:   A missing toolchain reports, it does not raise
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""A build handler with no toolchain must fail cleanly.

Running `hyperi-ci run build` on a Go project without Go raised
`FileNotFoundError: 'go'` and printed a rich traceback -- the CLI's own crash,
rather than the diagnosis. Python's handler has always reported "pytest not
installed"; Go and Rust now match. TypeScript already did, via
`ensure_pm_available`.
"""

from __future__ import annotations

from unittest.mock import patch

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.golang import build as go_build
from hyperi_ci.languages.rust import build as rust_build


class TestMissingToolchain:
    """The guard returns an exit code instead of letting subprocess raise."""

    def test_go_reports_rather_than_raising(self) -> None:
        with patch("hyperi_ci.languages.golang.build.shutil.which", return_value=None):
            with patch("hyperi_ci.languages.golang.build.subprocess.run") as spawned:
                assert go_build.run(CIConfig(_raw={})) == 1
        spawned.assert_not_called()

    def test_rust_reports_rather_than_raising(self) -> None:
        with patch("hyperi_ci.languages.rust.build.shutil.which", return_value=None):
            with patch("hyperi_ci.languages.rust.build.subprocess.run") as spawned:
                assert rust_build.run(CIConfig(_raw={})) == 1
        spawned.assert_not_called()

    def test_go_proceeds_when_the_toolchain_is_present(self) -> None:
        """The guard must not become the reason a real build never starts."""
        with patch(
            "hyperi_ci.languages.golang.build.shutil.which", return_value="/usr/bin/go"
        ):
            with patch(
                "hyperi_ci.languages.golang.build._detect_binary_name",
                return_value="app",
            ):
                with patch(
                    "hyperi_ci.languages.golang.build.subprocess.run"
                ) as spawned:
                    spawned.return_value.returncode = 0
                    go_build.run(CIConfig(_raw={}))
        assert spawned.called
