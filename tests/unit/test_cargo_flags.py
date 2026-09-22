# Project:   HyperI CI
# File:      tests/unit/test_cargo_flags.py
# Purpose:   Tests for the rustflags-that-never-arrive detector
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rustflags detector tests.

Both halves are silent failures, so the cases that matter are the ones that
must NOT fire: a correct `.cargo/*` negation, and flags under a target table.
A detector that cries wolf on the correct form is worse than no detector.
"""

from pathlib import Path

from hyperi_ci.quality.cargo_flags import _negations_under_excluded_dirs, scan

_BUILD_TABLE = """\
[build]
rustflags = ["-C", "target-cpu=x86-64-v3"]
"""

_TARGET_TABLE = """\
[target.x86_64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=x86-64-v3"]
"""


class TestTheInertNegation:
    """`.cargo/` excludes the DIRECTORY, so a negation under it does nothing."""

    def test_a_directory_exclude_makes_the_negation_inert(self) -> None:
        found = _negations_under_excluded_dirs(".cargo/\n!.cargo/config.toml\n")
        assert found == [(2, ".cargo/", ".cargo/config.toml")]

    def test_globbing_the_contents_is_the_working_form(self) -> None:
        """`.cargo/*` excludes the contents, which a negation CAN escape."""
        assert _negations_under_excluded_dirs(".cargo/*\n!.cargo/config.toml\n") == []

    def test_a_negation_with_no_matching_exclude_is_not_flagged(self) -> None:
        assert _negations_under_excluded_dirs("target/\n!src/keep.rs\n") == []

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        text = "# a comment\n\n.cargo/\n\n!.cargo/config.toml\n"
        assert len(_negations_under_excluded_dirs(text)) == 1


class TestScanningARepo:
    def test_an_untracked_negated_config_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text(
            ".cargo/\n!.cargo/config.toml\n", encoding="utf-8"
        )
        found = scan(tmp_path)
        assert [f.rule for f in found] == ["cargo/inert-gitignore-negation"]
        assert ".cargo/*" in found[0].message

    def test_flags_under_build_are_reported(self, tmp_path: Path) -> None:
        cargo = tmp_path / ".cargo"
        cargo.mkdir()
        (cargo / "config.toml").write_text(_BUILD_TABLE, encoding="utf-8")
        found = scan(tmp_path)
        assert [f.rule for f in found] == ["cargo/rustflags-under-build"]
        assert "DISCARDED" in found[0].message

    def test_flags_under_a_target_table_are_left_alone(self, tmp_path: Path) -> None:
        """Matching target entries JOIN, so this form is safe and must not fire."""
        cargo = tmp_path / ".cargo"
        cargo.mkdir()
        (cargo / "config.toml").write_text(_TARGET_TABLE, encoding="utf-8")
        assert scan(tmp_path) == []

    def test_a_clean_repo_reports_nothing(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("target/\n", encoding="utf-8")
        assert scan(tmp_path) == []

    def test_unparseable_toml_does_not_raise(self, tmp_path: Path) -> None:
        """A broken config is someone else's error to report, not a crash here."""
        cargo = tmp_path / ".cargo"
        cargo.mkdir()
        (cargo / "config.toml").write_text("[build\nrustflags =", encoding="utf-8")
        assert scan(tmp_path) == []
