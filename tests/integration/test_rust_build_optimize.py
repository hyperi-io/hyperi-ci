# Project:   HyperI CI
# File:      tests/integration/test_rust_build_optimize.py
# Purpose:   Integration tests for Rust release-track build optimisation
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Integration tests — build a real minimal Rust fixture crate and verify
the resulting binary has the expected optimisation characteristics.

These tests are SLOW (first compile can take 30-90s for jemalloc).
Mark them with @pytest.mark.slow so they're opt-in:

    uv run pytest tests/integration/test_rust_build_optimize.py -m slow

Skipped automatically if cargo is not on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hyperi_ci.languages.rust.optimize import (
    OptimizationProfile,
    resolve_optimization_profile,
    validate_profile,
)

pytestmark = pytest.mark.slow

CARGO_AVAILABLE = shutil.which("cargo") is not None
NM_AVAILABLE = shutil.which("nm") is not None
IS_LINUX = os.uname().sysname == "Linux"

# Sensible time limits — jemalloc compile is slow on first run, cached is fast.
CARGO_BUILD_TIMEOUT = 300  # 5 min


def _native_target() -> str:
    """Return the host target triple from rustc."""
    result = subprocess.run(
        ["rustc", "-vV"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("Could not determine native target from rustc -vV")


def _write_fixture_crate(
    root: Path,
    *,
    with_jemalloc_feature: bool,
    wire_global_allocator: bool,
) -> None:
    """Create a minimal Rust binary crate for optimisation testing.

    Args:
        root: Directory to scaffold into (usually tmp_path).
        with_jemalloc_feature: If True, declares `jemalloc` feature in Cargo.toml
                               with the tikv-jemallocator dep.
        wire_global_allocator: If True, wires #[global_allocator] in main.rs
                               under #[cfg(feature = "jemalloc")].
    """
    cargo_toml = [
        "[package]",
        'name = "fixture-bin"',
        'version = "0.1.0"',
        'edition = "2021"',
        "",
        "[dependencies]",
    ]
    if with_jemalloc_feature:
        cargo_toml.append('tikv-jemallocator = { version = "0.6", optional = true }')
    cargo_toml.extend(
        [
            "",
            "[features]",
            "default = []",
        ]
    )
    if with_jemalloc_feature:
        cargo_toml.append('jemalloc = ["dep:tikv-jemallocator"]')
    cargo_toml.extend(
        [
            "",
            "[profile.release]",
            'lto = "thin"',
            "codegen-units = 1",
            "strip = false",  # Keep symbols so nm can find them
        ]
    )

    main_rs = [
        "fn main() {",
        '    println!("fixture-bin");',
        "}",
    ]
    if with_jemalloc_feature and wire_global_allocator:
        main_rs = [
            '#[cfg(feature = "jemalloc")]',
            "#[global_allocator]",
            "static GLOBAL: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;",
            "",
            *main_rs,
        ]

    (root / "Cargo.toml").write_text("\n".join(cargo_toml) + "\n")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "main.rs").write_text("\n".join(main_rs) + "\n")


def _nm_has_symbol(binary: Path, substring: str) -> bool:
    """Return True if `nm` output for the binary contains `substring`."""
    result = subprocess.run(
        ["nm", "-D", "--defined-only", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    if substring in combined:
        return True
    # Try without --defined-only for cases where symbols are static
    result = subprocess.run(
        ["nm", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )
    return substring in (result.stdout + result.stderr)


def _run_cargo_build(
    project_dir: Path,
    profile: OptimizationProfile,
) -> subprocess.CompletedProcess[str]:
    """Run `cargo build --release` for the fixture with profile applied.

    Mirrors the flag + env injection that build.py does, without going
    through the full run()/dispatch path (which includes cross-compile
    sysroot setup we don't need here).
    """
    features = profile.cargo_features()
    cmd = ["cargo", "build", "--release"]
    if features:
        cmd.extend(["--features", ",".join(features)])

    env = dict(os.environ)
    env.update(profile.env_overrides())

    return subprocess.run(
        cmd,
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=CARGO_BUILD_TIMEOUT,
        check=False,
    )


@pytest.mark.skipif(not CARGO_AVAILABLE, reason="cargo not installed")
@pytest.mark.skipif(not NM_AVAILABLE, reason="nm not installed")
@pytest.mark.skipif(not IS_LINUX, reason="jemalloc symbol check is Linux-specific")
class TestTier1AllocatorEffect:
    """Prove that profile.allocator actually ends up in the compiled binary."""

    def test_jemalloc_allocator_links_jemalloc_symbols(self, tmp_path) -> None:
        _write_fixture_crate(
            tmp_path,
            with_jemalloc_feature=True,
            wire_global_allocator=True,
        )
        profile = OptimizationProfile(
            channel="release",
            allocator="jemalloc",
            lto="thin",  # Keep thin to avoid LTO compile-time blowup
        )

        result = _run_cargo_build(tmp_path, profile)
        assert result.returncode == 0, (
            f"cargo build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        binary = tmp_path / "target" / "release" / "fixture-bin"
        assert binary.exists(), f"Binary not produced at {binary}"

        # jemalloc exports symbols like je_malloc, _rjem_je_malloc, etc.
        # tikv-jemallocator uses the "je_" prefix by default.
        assert _nm_has_symbol(binary, "je_"), (
            "Expected jemalloc symbols (je_*) in binary but found none — "
            "profile.allocator=jemalloc did not actually link jemalloc"
        )

    def test_system_allocator_no_jemalloc_symbols(self, tmp_path) -> None:
        # Same fixture, but build with allocator=system — feature not passed,
        # jemalloc dep never compiles in.
        _write_fixture_crate(
            tmp_path,
            with_jemalloc_feature=True,  # Available, but we don't select it
            wire_global_allocator=True,
        )
        profile = OptimizationProfile(
            channel="release",
            allocator="system",
            lto="thin",
        )

        result = _run_cargo_build(tmp_path, profile)
        assert result.returncode == 0, (
            f"cargo build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        binary = tmp_path / "target" / "release" / "fixture-bin"
        assert binary.exists()

        # System allocator means no jemalloc pull-in — no je_ symbols
        assert not _nm_has_symbol(binary, "je_malloc"), (
            "Found jemalloc symbols in binary despite allocator=system — "
            "system allocator path is leaking jemalloc"
        )


@pytest.mark.skipif(not CARGO_AVAILABLE, reason="cargo not installed")
class TestTier1LTOEffect:
    """Prove that CARGO_PROFILE_RELEASE_LTO env var is honoured by cargo."""

    def test_fat_lto_env_var_produces_binary(self, tmp_path) -> None:
        # Smoke test: setting CARGO_PROFILE_RELEASE_LTO=fat at build time
        # doesn't error out and produces a binary. Verifying fat vs thin
        # requires binary size comparison across two builds — that's
        # flaky across rustc versions, so we just verify the env var
        # is accepted.
        _write_fixture_crate(
            tmp_path,
            with_jemalloc_feature=False,
            wire_global_allocator=False,
        )
        profile = OptimizationProfile(
            channel="release",
            allocator="system",
            lto="fat",
        )

        result = _run_cargo_build(tmp_path, profile)
        assert result.returncode == 0, (
            f"cargo build with fat LTO failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert (tmp_path / "target" / "release" / "fixture-bin").exists()

    def test_thin_lto_env_var_produces_binary(self, tmp_path) -> None:
        _write_fixture_crate(
            tmp_path,
            with_jemalloc_feature=False,
            wire_global_allocator=False,
        )
        profile = OptimizationProfile(
            channel="spike",
            allocator="system",
            lto="thin",
        )

        result = _run_cargo_build(tmp_path, profile)
        assert result.returncode == 0
        assert (tmp_path / "target" / "release" / "fixture-bin").exists()


@pytest.mark.skipif(not CARGO_AVAILABLE, reason="cargo not installed")
@pytest.mark.skipif(not NM_AVAILABLE, reason="nm not installed")
@pytest.mark.skipif(not IS_LINUX, reason="symbol check is Linux-specific")
class TestChannelToBinaryFlow:
    """End-to-end: channel + user config → resolver → validated profile →
    cargo build → binary with expected optimisation characteristics."""

    def test_release_channel_default_links_jemalloc(self, tmp_path) -> None:
        # No user config — release channel should default to jemalloc.
        _write_fixture_crate(
            tmp_path,
            with_jemalloc_feature=True,
            wire_global_allocator=True,
        )

        profile = resolve_optimization_profile("release", None)
        validated = validate_profile(
            profile,
            cargo_features={"default", "jemalloc"},
            target=_native_target(),
        )
        # LTO=thin to speed up the integration test
        fast_profile = OptimizationProfile(
            channel=validated.channel,
            allocator=validated.allocator,
            lto="thin",
        )
        assert fast_profile.allocator == "jemalloc"

        result = _run_cargo_build(tmp_path, fast_profile)
        assert result.returncode == 0

        binary = tmp_path / "target" / "release" / "fixture-bin"
        assert _nm_has_symbol(binary, "je_")

    def test_spike_channel_default_uses_system_allocator(self, tmp_path) -> None:
        # spike channel should default to system allocator — no jemalloc.
        _write_fixture_crate(
            tmp_path,
            with_jemalloc_feature=True,  # Available but not selected
            wire_global_allocator=True,
        )

        profile = resolve_optimization_profile("spike", None)
        assert profile.allocator == "system"

        result = _run_cargo_build(tmp_path, profile)
        assert result.returncode == 0

        binary = tmp_path / "target" / "release" / "fixture-bin"
        assert not _nm_has_symbol(binary, "je_malloc")


@pytest.mark.skipif(not CARGO_AVAILABLE, reason="cargo not installed")
class TestValidateFallback:
    """Graceful fallback: when jemalloc requested but not declared in
    Cargo.toml, validate_profile falls back to system and the subsequent
    build succeeds without jemalloc."""

    def test_missing_feature_falls_back_and_builds(self, tmp_path) -> None:
        # Fixture with NO jemalloc feature declared
        _write_fixture_crate(
            tmp_path,
            with_jemalloc_feature=False,
            wire_global_allocator=False,
        )

        # Request jemalloc but it's not declared — validator should fall back
        raw_profile = OptimizationProfile(
            channel="release",
            allocator="jemalloc",
            lto="thin",
        )
        validated = validate_profile(
            raw_profile,
            cargo_features=set(),  # Nothing declared
            target=_native_target(),
        )
        assert validated.allocator == "system"
        assert any("jemalloc" in w for w in validated.warnings)

        # Build with the fallback profile — should succeed
        result = _run_cargo_build(tmp_path, validated)
        assert result.returncode == 0, (
            f"Fallback build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
