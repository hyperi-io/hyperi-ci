# Project:   HyperI CI
# File:      tests/unit/test_build_targets.py
# Purpose:   The Rust targets a project lists, as the Plan matrix reads them
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``build.rust.targets``, read without hyperi-ci installed (issue #291)."""

from pathlib import Path

import pytest

from hyperi_ci.build_targets import (
    ci_targets,
    declared_targets,
    os_legs,
    os_test_runners,
    read_rust_targets,
    unbuilt_targets,
)

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


@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        (None, (_AMD64, _ARM64)),
        ([], (_AMD64, _ARM64)),
        ([_ARM64], (_ARM64,)),
        (_AMD64, (_AMD64, _ARM64)),  # unreadable: the default pair
    ],
)
def test_ci_targets_are_the_list_else_the_linux_pair(
    targets: object, expected: tuple[str, ...]
) -> None:
    assert ci_targets({"build": {"rust": {"targets": targets}}}) == expected


_WINDOWS = "x86_64-pc-windows-msvc"
_MAC_ARM = "aarch64-apple-darwin"
_MAC_INTEL = "x86_64-apple-darwin"


@pytest.mark.parametrize(
    ("targets", "legs", "unbuilt"),
    [
        ([], [], []),
        ([_AMD64, _ARM64], [], []),
        (
            [_WINDOWS, _AMD64, _WINDOWS],
            [(_WINDOWS, "windows-2025", "windows-amd64")],
            [],
        ),
        (
            [_MAC_ARM, _MAC_INTEL],
            [
                (_MAC_ARM, "macos-26", "darwin-arm64"),
                (_MAC_INTEL, "macos-26-intel", "darwin-amd64"),
            ],
            [],
        ),
        (
            ["aarch64-pc-windows-msvc", "x86_64-unknown-linux-musl"],
            [],
            ["aarch64-pc-windows-msvc", "x86_64-unknown-linux-musl"],
        ),
    ],
)
def test_os_legs_cover_windows_and_macos_only(
    targets: list[str], legs: list[tuple[str, str, str]], unbuilt: list[str]
) -> None:
    expected = [{"target": t, "runner": r, "os_arch": o} for t, r, o in legs]
    assert os_legs(targets) == expected
    assert unbuilt_targets(targets) == unbuilt


@pytest.mark.parametrize(
    ("override", "runner"),
    [("", "windows-2025"), ("windows-devex", "windows-devex"), (" ", "windows-2025")],
)
def test_windows_runner_override_moves_only_the_windows_leg(
    override: str, runner: str
) -> None:
    legs = os_legs([_WINDOWS, _MAC_ARM], override.strip())
    assert [leg["runner"] for leg in legs] == [runner, "macos-26"]


_LEGS = os_legs([_WINDOWS, _MAC_ARM, _MAC_INTEL], "windows-devex")


@pytest.mark.parametrize(
    ("private", "event", "ref", "runners"),
    [
        (True, "pull_request", "refs/pull/7/merge", ["windows-devex"]),
        (True, "push", "refs/heads/feature", ["windows-devex"]),
        (
            True,
            "push",
            "refs/heads/main",
            ["windows-devex", "macos-26", "macos-26-intel"],
        ),
        (
            True,
            "merge_group",
            "refs/heads/gh-readonly-queue/main/x",
            ["windows-devex", "macos-26", "macos-26-intel"],
        ),
        (
            True,
            "schedule",
            "refs/heads/main",
            ["windows-devex", "macos-26", "macos-26-intel"],
        ),
        (
            True,
            "workflow_dispatch",
            "refs/heads/feature",
            ["windows-devex", "macos-26", "macos-26-intel"],
        ),
        (
            False,
            "pull_request",
            "refs/pull/7/merge",
            ["windows-devex", "macos-26", "macos-26-intel"],
        ),
    ],
)
def test_hosted_os_test_legs_skip_per_commit_runs_on_a_private_repo(
    private: bool, event: str, ref: str, runners: list[str]
) -> None:
    assert os_test_runners(_LEGS, private=private, event=event, ref=ref) == runners


@pytest.mark.parametrize(
    "label", ["windows-latest", "windows-2025", "macos-latest", "macos-15"]
)
def test_every_hosted_label_skips_a_private_pull_request(label: str) -> None:
    legs = [{"target": _WINDOWS, "runner": label, "os_arch": "x"}]
    assert os_test_runners(legs, private=True, event="pull_request", ref="") == []


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
