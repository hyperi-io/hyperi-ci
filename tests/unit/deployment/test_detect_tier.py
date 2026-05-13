# Project:   HyperI CI
# File:      tests/unit/deployment/test_detect_tier.py
# Purpose:   Tier auto-detection tests
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.deployment.detect.detect_tier`."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.deployment.detect import Tier, detect_tier


class TestDetectTier:
    """Tier auto-detection covers the four expected outcomes."""

    def test_empty_dir_is_none(self, tmp_path: Path) -> None:
        assert detect_tier(tmp_path) == Tier.NONE

    def test_cargo_with_rustlib_is_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n[dependencies]\nhyperi-rustlib = "2.5"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_cargo_with_workspace_inheritance_is_rust(self, tmp_path: Path) -> None:
        # Real-world pattern from ci-test-rust-workspace/crates/cli/Cargo.toml.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n[dependencies]\nhyperi-rustlib.workspace = true\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_cargo_with_table_form_is_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n'
            "[dependencies]\n"
            'hyperi-rustlib = { version = "2.5", features = ["cli"] }\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_cargo_without_rustlib_is_not_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n[dependencies]\nserde = "1"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_pyproject_with_pylib_is_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["hyperi-pylib>=2.24"]\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.PYTHON

    def test_pyproject_with_pylib_extras_is_python(self, tmp_path: Path) -> None:
        # The metrics extra string still contains "hyperi-pylib".
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["hyperi-pylib[metrics]>=2.24"]\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.PYTHON

    def test_pyproject_without_pylib_is_not_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["pyyaml>=6"]\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_only_contract_json_is_other(self, tmp_path: Path) -> None:
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "deployment-contract.json").write_text(
            '{"app_name": "x"}', encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.OTHER

    def test_rust_wins_over_other(self, tmp_path: Path) -> None:
        # If a repo somehow has both Cargo.toml AND ci/deployment-contract.json,
        # rust takes precedence — the producer chain is more authoritative
        # than the manually maintained JSON.
        (tmp_path / "Cargo.toml").write_text(
            '[dependencies]\nhyperi-rustlib = "2.5"\n', encoding="utf-8"
        )
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "deployment-contract.json").write_text(
            '{"app_name": "x"}', encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_rust_wins_over_python(self, tmp_path: Path) -> None:
        # Some repos vendor both. Rust is the main producer; pylib is for
        # the integrations subdir. Rust takes precedence.
        (tmp_path / "Cargo.toml").write_text(
            '[dependencies]\nhyperi-rustlib = "2.5"\n', encoding="utf-8"
        )
        (tmp_path / "pyproject.toml").write_text(
            'dependencies = ["hyperi-pylib>=2.24"]\n', encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_python_wins_over_other(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["hyperi-pylib>=2.24"]\n',
            encoding="utf-8",
        )
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "deployment-contract.json").write_text(
            '{"app_name": "x"}', encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.PYTHON


class TestSelfMatchExclusion:
    """A library's own repo is NOT a Tier 1/2 consumer of itself.

    These tests cover the case where a library's Cargo.toml /
    pyproject.toml has its own package name (e.g. `hyperi-rustlib`)
    in the `[package]` / `[project]` table. The substring match would
    otherwise misdetect the library as a consumer and dispatch the
    Tier 1/2 producer, which then fails with "no binary found".
    """

    def test_hyperi_rustlib_own_repo_is_not_rust(self, tmp_path: Path) -> None:
        # The library's own Cargo.toml has `name = "hyperi-rustlib"` in
        # [package] but no `hyperi-rustlib = ...` dep line.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "hyperi-rustlib"\nversion = "2.7.0"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_hyperi_rustlib_with_self_in_dev_deps_still_excluded(
        self, tmp_path: Path
    ) -> None:
        # Hypothetical: library lists its own name in [dev-dependencies]
        # for an example-binary workspace pattern. Still not a consumer.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "hyperi-rustlib"\nversion = "2.7.0"\n'
            '[dev-dependencies]\nhyperi-rustlib = { path = "." }\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_hyperi_pylib_own_repo_is_not_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "hyperi-pylib"\nversion = "2.24.0"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_hyperi_pylib_poetry_section_also_excluded(self, tmp_path: Path) -> None:
        # Poetry-managed projects use [tool.poetry] instead of [project].
        (tmp_path / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "hyperi-pylib"\nversion = "2.24.0"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_single_quoted_name_excluded(self, tmp_path: Path) -> None:
        # TOML allows single-quoted strings — must still match.
        (tmp_path / "Cargo.toml").write_text(
            "[package]\nname = 'hyperi-rustlib'\nversion = '2.7.0'\n",
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_consumer_with_real_dep_still_detected(self, tmp_path: Path) -> None:
        # A real consumer has its own name AND lists the library as a dep.
        # Self-match exclusion must not break this case.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "dfe-loader"\n[dependencies]\nhyperi-rustlib = "2.5"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_consumer_with_similar_prefix_not_misdetected(self, tmp_path: Path) -> None:
        # `name = "hyperi-rustlib-extras"` should NOT count as self-match
        # for `hyperi-rustlib` because the full string differs. This
        # consumer DOES depend on hyperi-rustlib.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "hyperi-rustlib-extras"\n'
            '[dependencies]\nhyperi-rustlib = "2.5"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_name_with_trailing_comment_handled(self, tmp_path: Path) -> None:
        # Inline comments after the name field shouldn't break parsing.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "hyperi-rustlib"  # the library itself\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_name_outside_recognised_section_not_self_match(
        self, tmp_path: Path
    ) -> None:
        # A `name = "hyperi-rustlib"` in some other table (e.g.
        # [features.something]) shouldn't count as the manifest's own
        # name. We only treat [package] / [project] / [tool.poetry] as
        # self-name sections.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "consumer-app"\n'
            "[features.weird]\n"
            'name = "hyperi-rustlib"\n'
            '[dependencies]\nhyperi-rustlib = "2.5"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST


class TestTierEnum:
    """`Tier` enum has the four values expected by callers."""

    @pytest.mark.parametrize(
        "name,value",
        [
            ("RUST", "rust"),
            ("PYTHON", "python"),
            ("OTHER", "other"),
            ("NONE", "none"),
        ],
    )
    def test_tier_values(self, name: str, value: str) -> None:
        assert getattr(Tier, name).value == value

    def test_tier_string_compatible(self) -> None:
        # StrEnum lets callers compare directly to a string value.
        assert Tier.RUST == "rust"
        assert "python" == Tier.PYTHON
