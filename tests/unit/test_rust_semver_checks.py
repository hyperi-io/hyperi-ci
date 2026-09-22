# Project:   HyperI CI
# File:      tests/unit/test_rust_semver_checks.py
# Purpose:   Tests for the public-API break check
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Public-API break check tests.

The case that matters most is the skip: a binary-only crate must be reported
as having nothing to check, never as having been checked and found clean.
"""

from pathlib import Path

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import semver_checks


def _config(mode: str = "warn") -> CIConfig:
    return CIConfig(
        publish_target="oss",
        _raw={"quality": {"rust": {"semver_checks": mode}}},
    )


class TestItSkipsWhatItCannotCheck:
    def test_disabled_does_not_run(self, tmp_path: Path) -> None:
        assert semver_checks.run(_config("disabled"), project_root=tmp_path) == 0

    def test_a_binary_only_crate_is_skipped(self, tmp_path: Path) -> None:
        """No lib target means no public API -- and that must not read as a pass."""
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "app"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        assert semver_checks.run(_config(), project_root=tmp_path) == 0

    def test_no_manifest_is_skipped(self, tmp_path: Path) -> None:
        assert semver_checks.run(_config(), project_root=tmp_path) == 0

    # The live path belongs to the fixture fleet: the tool resolves its
    # baseline from crates.io, so driving it here is a network wait.
