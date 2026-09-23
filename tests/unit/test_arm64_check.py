# Project:   HyperI CI
# File:      tests/unit/test_arm64_check.py
# Purpose:   The project half of the arm64-parity gate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Which projects owe an arm64 build on a release-worthy merge (issue #249)."""

from pathlib import Path

import pytest

from hyperi_ci.arm64_check import AARCH64, wants_arm64_check
from hyperi_ci.config import packaged_default


def _project(root: Path, *, cargo: bool = True, config: str | None = None) -> Path:
    if cargo:
        (root / "Cargo.toml").write_text(
            '[package]\nname = "thing"\n', encoding="utf-8"
        )
    if config is not None:
        (root / ".hyperi-ci.yaml").write_text(config, encoding="utf-8")
    return root


def test_a_rust_project_with_no_config_is_on(tmp_path: Path) -> None:
    # No targets list means every target, so aarch64 is among them.
    wanted, reason = wants_arm64_check(_project(tmp_path))
    assert wanted is True
    assert AARCH64 in reason


def test_the_shipped_default_matches_what_the_code_assumes(tmp_path: Path) -> None:
    # `hyperi-ci config` reads defaults.yaml, so the key has to be there, with
    # the value an unset key already gets from wants_arm64_check.
    assert packaged_default("build.rust.arm64_on_main") is True
    assert wants_arm64_check(_project(tmp_path))[0] is True


def test_a_non_rust_project_is_off(tmp_path: Path) -> None:
    # python/ts/go workflows never read the output, but the gate's summary
    # line would read as a promise the run did not keep.
    wanted, reason = wants_arm64_check(_project(tmp_path, cargo=False))
    assert wanted is False
    assert "Cargo.toml" in reason


def test_an_empty_config_is_on(tmp_path: Path) -> None:
    wanted, _ = wants_arm64_check(_project(tmp_path, config=""))
    assert wanted is True


@pytest.mark.parametrize("value", ["false", "False", "no", "off", "0", '"false"'])
def test_the_opt_out_is_honoured_however_it_is_spelled(
    tmp_path: Path, value: str
) -> None:
    config = f"build:\n  rust:\n    arm64_on_main: {value}\n"
    wanted, reason = wants_arm64_check(_project(tmp_path, config=config))
    assert wanted is False, f"{value!r} did not opt the project out"
    assert "arm64_on_main" in reason


@pytest.mark.parametrize("value", ["true", "yes"])
def test_an_explicit_opt_in_stays_on(tmp_path: Path, value: str) -> None:
    config = f"build:\n  rust:\n    arm64_on_main: {value}\n"
    wanted, _ = wants_arm64_check(_project(tmp_path, config=config))
    assert wanted is True


def test_a_project_that_ships_no_aarch64_is_off(tmp_path: Path) -> None:
    # issue #127: a project whose release build does not fit the arm64 runner
    # lists amd64 alone. Checking it on main would fail a leg it never ships.
    config = "build:\n  rust:\n    targets:\n      - x86_64-unknown-linux-gnu\n"
    wanted, reason = wants_arm64_check(_project(tmp_path, config=config))
    assert wanted is False
    assert "targets" in reason


def test_a_project_that_lists_aarch64_is_on(tmp_path: Path) -> None:
    config = (
        "build:\n  rust:\n    targets:\n"
        "      - x86_64-unknown-linux-gnu\n"
        f"      - {AARCH64}\n"
    )
    wanted, _ = wants_arm64_check(_project(tmp_path, config=config))
    assert wanted is True


def test_unrelated_config_does_not_confuse_it(tmp_path: Path) -> None:
    config = "quality:\n  rust:\n    clippy: blocking\nbuild:\n  enabled: true\n"
    wanted, _ = wants_arm64_check(_project(tmp_path, config=config))
    assert wanted is True


def test_an_unreadable_config_is_off(tmp_path: Path) -> None:
    # Spending an arm64 runner per releasable merge across the fleet on a
    # guess is worse than the missed check, which the gate's summary names.
    config = "build:\n  rust:\n   - this is not a mapping\n  bad indent\n"
    wanted, reason = wants_arm64_check(_project(tmp_path, config=config))
    assert wanted is False
    assert "could not be read" in reason


def test_a_config_that_is_not_a_mapping_is_off(tmp_path: Path) -> None:
    wanted, reason = wants_arm64_check(_project(tmp_path, config="- one\n- two\n"))
    assert wanted is False
    assert "could not be read" in reason
