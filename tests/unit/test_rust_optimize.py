# Project:   HyperI CI
# File:      tests/unit/test_rust_optimize.py
# Purpose:   Unit tests for Rust release-track optimisation profile resolver
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from hyperi_ci.languages.rust.optimize import (
    OptimizationProfile,
    _parse_features_from_text,
    parse_cargo_features,
    resolve_optimization_profile,
    validate_profile,
)


class TestChannelDefaults:
    """Channel-tiered defaults (Tier 1 = allocator + LTO per channel)."""

    def test_spike_uses_system_allocator_and_thin_lto(self) -> None:
        p = resolve_optimization_profile("spike", None)
        assert p.allocator == "system"
        assert p.lto == "thin"

    def test_alpha_uses_system_allocator_and_thin_lto(self) -> None:
        p = resolve_optimization_profile("alpha", None)
        assert p.allocator == "system"
        assert p.lto == "thin"

    def test_beta_enables_jemalloc_and_fat_lto(self) -> None:
        p = resolve_optimization_profile("beta", None)
        assert p.allocator == "jemalloc"
        assert p.lto == "fat"

    def test_release_enables_jemalloc_and_fat_lto(self) -> None:
        p = resolve_optimization_profile("release", None)
        assert p.allocator == "jemalloc"
        assert p.lto == "fat"

    def test_unknown_channel_falls_back_to_spike_defaults(self) -> None:
        p = resolve_optimization_profile("random-channel-name", None)
        assert p.allocator == "system"
        assert p.lto == "thin"


class TestUserOverrides:
    """Explicit user config overrides channel defaults."""

    def test_user_can_opt_out_of_jemalloc_at_release(self) -> None:
        p = resolve_optimization_profile("release", {"allocator": "system"})
        assert p.allocator == "system"

    def test_user_can_opt_out_of_fat_lto_at_release(self) -> None:
        p = resolve_optimization_profile("release", {"lto": "thin"})
        assert p.lto == "thin"

    def test_user_can_select_mimalloc(self) -> None:
        p = resolve_optimization_profile("release", {"allocator": "mimalloc"})
        assert p.allocator == "mimalloc"

    def test_unknown_allocator_string_falls_back_to_system(self) -> None:
        p = resolve_optimization_profile("release", {"allocator": "tcmalloc"})
        assert p.allocator == "system"

    def test_unknown_lto_string_falls_back_to_thin(self) -> None:
        p = resolve_optimization_profile("release", {"lto": "super-fat"})
        assert p.lto == "thin"


class TestPGOGating:
    """PGO requires channel=release AND user opt-in."""

    def test_pgo_disabled_by_default(self) -> None:
        p = resolve_optimization_profile("release", None)
        assert p.pgo_enabled is False

    def test_pgo_requires_release_channel(self) -> None:
        p = resolve_optimization_profile(
            "beta",
            {"pgo": {"enabled": True, "workload_cmd": "x"}},
        )
        assert p.pgo_enabled is False  # gated off on beta

    def test_pgo_enabled_at_release_when_opted_in(self) -> None:
        p = resolve_optimization_profile(
            "release",
            {"pgo": {"enabled": True, "workload_cmd": "bash x.sh"}},
        )
        assert p.pgo_enabled is True
        assert p.pgo_workload_cmd == "bash x.sh"

    def test_pgo_duration_default_is_300s(self) -> None:
        p = resolve_optimization_profile(
            "release",
            {"pgo": {"enabled": True, "workload_cmd": "x"}},
        )
        assert p.pgo_duration_secs == 300

    def test_pgo_duration_configurable(self) -> None:
        p = resolve_optimization_profile(
            "release",
            {"pgo": {"enabled": True, "workload_cmd": "x", "duration_secs": 600}},
        )
        assert p.pgo_duration_secs == 600


class TestBOLTGating:
    """BOLT requires channel=release AND PGO enabled AND user opt-in."""

    def test_bolt_disabled_by_default(self) -> None:
        p = resolve_optimization_profile("release", None)
        assert p.bolt_enabled is False

    def test_bolt_without_pgo_disabled(self) -> None:
        p = resolve_optimization_profile(
            "release",
            {"bolt": {"enabled": True}},
        )
        assert p.bolt_enabled is False  # no PGO → no BOLT

    def test_bolt_with_pgo_at_release_enabled(self) -> None:
        p = resolve_optimization_profile(
            "release",
            {
                "pgo": {"enabled": True, "workload_cmd": "x"},
                "bolt": {"enabled": True},
            },
        )
        assert p.bolt_enabled is True

    def test_bolt_at_beta_disabled(self) -> None:
        p = resolve_optimization_profile(
            "beta",
            {
                "pgo": {"enabled": True, "workload_cmd": "x"},
                "bolt": {"enabled": True},
            },
        )
        assert p.bolt_enabled is False  # not release channel


class TestCargoFeatures:
    """`cargo_features()` returns the feature list for `--features` flag."""

    def test_system_allocator_returns_empty_list(self) -> None:
        p = OptimizationProfile(channel="spike", allocator="system")
        assert p.cargo_features() == []

    def test_jemalloc_returns_jemalloc_feature(self) -> None:
        p = OptimizationProfile(channel="release", allocator="jemalloc")
        assert p.cargo_features() == ["jemalloc"]

    def test_mimalloc_returns_mimalloc_feature(self) -> None:
        p = OptimizationProfile(channel="release", allocator="mimalloc")
        assert p.cargo_features() == ["mimalloc"]


class TestEnvOverrides:
    """`env_overrides()` injects CARGO_PROFILE_RELEASE_LTO at build time."""

    def test_fat_lto_sets_env_var(self) -> None:
        p = OptimizationProfile(channel="release", lto="fat")
        assert p.env_overrides() == {"CARGO_PROFILE_RELEASE_LTO": "fat"}

    def test_thin_lto_sets_env_var(self) -> None:
        p = OptimizationProfile(channel="spike", lto="thin")
        assert p.env_overrides() == {"CARGO_PROFILE_RELEASE_LTO": "thin"}


class TestValidateProfile:
    """Graceful fallbacks when Cargo.toml / target don't support requested opts."""

    def test_missing_jemalloc_feature_falls_back_to_system(self) -> None:
        p = OptimizationProfile(channel="release", allocator="jemalloc")
        validated = validate_profile(p, cargo_features=set())
        assert validated.allocator == "system"
        assert any("jemalloc" in w for w in validated.warnings)

    def test_present_jemalloc_feature_keeps_jemalloc(self) -> None:
        p = OptimizationProfile(channel="release", allocator="jemalloc")
        validated = validate_profile(p, cargo_features={"jemalloc"})
        assert validated.allocator == "jemalloc"
        assert not validated.warnings

    def test_missing_mimalloc_feature_falls_back_to_system(self) -> None:
        p = OptimizationProfile(channel="release", allocator="mimalloc")
        validated = validate_profile(p, cargo_features={"jemalloc"})
        assert validated.allocator == "system"

    def test_system_allocator_never_needs_feature_check(self) -> None:
        p = OptimizationProfile(channel="release", allocator="system")
        validated = validate_profile(p, cargo_features=set())
        assert validated.allocator == "system"
        assert not validated.warnings

    def test_pgo_without_workload_cmd_is_disabled(self) -> None:
        p = OptimizationProfile(
            channel="release",
            allocator="jemalloc",
            pgo_enabled=True,
            pgo_workload_cmd=None,
        )
        validated = validate_profile(p, cargo_features={"jemalloc"})
        assert validated.pgo_enabled is False
        assert any("workload_cmd" in w for w in validated.warnings)

    def test_bolt_on_non_linux_target_disabled(self) -> None:
        p = OptimizationProfile(
            channel="release",
            allocator="jemalloc",
            pgo_enabled=True,
            pgo_workload_cmd="bash x.sh",
            bolt_enabled=True,
        )
        validated = validate_profile(
            p,
            cargo_features={"jemalloc"},
            target="x86_64-apple-darwin",
        )
        assert validated.bolt_enabled is False
        assert any("BOLT" in w or "bolt" in w for w in validated.warnings)

    def test_bolt_on_linux_target_kept(self) -> None:
        p = OptimizationProfile(
            channel="release",
            allocator="jemalloc",
            pgo_enabled=True,
            pgo_workload_cmd="bash x.sh",
            bolt_enabled=True,
        )
        validated = validate_profile(
            p,
            cargo_features={"jemalloc"},
            target="aarch64-unknown-linux-gnu",
        )
        assert validated.bolt_enabled is True

    def test_pgo_workload_failure_disables_bolt_too(self) -> None:
        # PGO disabled because missing workload → BOLT (which needs PGO) disabled too
        p = OptimizationProfile(
            channel="release",
            allocator="jemalloc",
            pgo_enabled=True,
            pgo_workload_cmd=None,
            bolt_enabled=True,
        )
        validated = validate_profile(p, cargo_features={"jemalloc"})
        assert validated.bolt_enabled is False


class TestParseCargoFeatures:
    """Cargo.toml feature parsing (stdlib-only)."""

    def test_empty_toml_returns_empty_set(self) -> None:
        assert _parse_features_from_text("") == set()

    def test_no_features_section_returns_empty_set(self) -> None:
        text = '[package]\nname = "foo"\nversion = "0.1.0"\n'
        assert _parse_features_from_text(text) == set()

    def test_simple_features_parsed(self) -> None:
        text = """
[package]
name = "foo"

[features]
default = []
jemalloc = ["dep:tikv-jemallocator"]
mimalloc = ["dep:mimalloc"]
"""
        assert _parse_features_from_text(text) == {"default", "jemalloc", "mimalloc"}

    def test_features_ignored_outside_section(self) -> None:
        text = """
[features]
jemalloc = []

[dependencies]
default = { version = "1.0" }
"""
        assert _parse_features_from_text(text) == {"jemalloc"}

    def test_comments_ignored(self) -> None:
        text = """
[features]
# This is a comment
default = []
# mimalloc = []
mimalloc = []
"""
        assert _parse_features_from_text(text) == {"default", "mimalloc"}

    def test_parse_cargo_features_missing_file_returns_empty(self, tmp_path) -> None:
        assert parse_cargo_features(tmp_path / "nonexistent.toml") == set()

    def test_parse_cargo_features_reads_real_file(self, tmp_path) -> None:
        cargo_toml = tmp_path / "Cargo.toml"
        cargo_toml.write_text(
            '[package]\nname = "x"\n\n[features]\nfoo = []\nbar = []\n'
        )
        assert parse_cargo_features(cargo_toml) == {"foo", "bar"}


class TestDescribe:
    """`describe()` produces the one-line CI log summary."""

    def test_describe_tier1_release(self) -> None:
        p = OptimizationProfile(channel="release", allocator="jemalloc", lto="fat")
        assert p.describe() == "channel=release, allocator=jemalloc, lto=fat"

    def test_describe_includes_pgo_when_enabled(self) -> None:
        p = OptimizationProfile(
            channel="release",
            allocator="jemalloc",
            lto="fat",
            pgo_enabled=True,
            pgo_workload_cmd="x",
        )
        assert "pgo=on" in p.describe()

    def test_describe_includes_bolt_when_enabled(self) -> None:
        p = OptimizationProfile(
            channel="release",
            allocator="jemalloc",
            lto="fat",
            pgo_enabled=True,
            pgo_workload_cmd="x",
            bolt_enabled=True,
        )
        s = p.describe()
        assert "pgo=on" in s
        assert "bolt=on" in s
