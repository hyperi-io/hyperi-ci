# Project:   HyperI CI
# File:      tests/unit/test_build_targets.py
# Purpose:   The Rust targets a project lists, as the Plan matrix reads them
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``build.rust.targets``, read without hyperi-ci installed (issue #291)."""

from pathlib import Path

import pytest

from hyperi_ci.build_targets import declared_targets, read_rust_targets

_AMD64 = "x86_64-unknown-linux-gnu"
_ARM64 = "aarch64-unknown-linux-gnu"


class TestDeclaredTargets:
    @pytest.mark.parametrize(
        "config",
        [
            {},
            {"build": None},
            {"build": {"rust": None}},
            {"build": {"rust": {"targets": None}}},
            {"build": {"rust": {"targets": []}}},
        ],
    )
    def test_nothing_listed_is_empty(self, config: dict) -> None:
        assert declared_targets(config) == []

    def test_the_list_keeps_its_order(self) -> None:
        config = {"build": {"rust": {"targets": [_ARM64, _AMD64]}}}
        assert declared_targets(config) == [_ARM64, _AMD64]

    def test_blank_entries_are_dropped(self) -> None:
        config = {"build": {"rust": {"targets": [f" {_AMD64} ", ""]}}}
        assert declared_targets(config) == [_AMD64]

    @pytest.mark.parametrize("value", [_AMD64, {"a": 1}, [_AMD64, 3]])
    def test_anything_but_a_list_of_strings_is_refused(self, value: object) -> None:
        with pytest.raises(ValueError, match="build.rust.targets"):
            declared_targets({"build": {"rust": {"targets": value}}})


class TestReadRustTargets:
    def test_no_config_means_every_target(self, tmp_path: Path) -> None:
        assert read_rust_targets(tmp_path) == ([], "")

    def test_a_listed_target_is_read(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yml").write_text(
            f"build:\n  rust:\n    targets: [{_AMD64}]\n", encoding="utf-8"
        )
        assert read_rust_targets(tmp_path) == ([_AMD64], "")

    def test_an_unreadable_config_names_the_file(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("build: [\n", encoding="utf-8")
        targets, problem = read_rust_targets(tmp_path)
        assert targets == []
        assert problem.startswith(".hyperi-ci.yaml could not be read:")

    def test_a_malformed_list_names_the_file(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            f"build:\n  rust:\n    targets: {_AMD64}\n", encoding="utf-8"
        )
        targets, problem = read_rust_targets(tmp_path)
        assert targets == []
        assert problem.startswith(".hyperi-ci.yaml: build.rust.targets must be a list")
