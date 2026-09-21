# Project:   HyperI CI
# File:      tests/unit/test_rust_optimize.py
# Purpose:   Unit tests for Rust release-track optimisation profile resolver
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import optimize
from hyperi_ci.languages.rust.build import _resolve_build_channel
from hyperi_ci.languages.rust.optimize import (
    OptimizationOutcome,
    OptimizationProfile,
    _parse_features_from_text,
    cargo_feature_args,
    log_outcome,
    parse_cargo_features,
    resolve_optimization_profile,
    unoptimized_release_refusal,
    validate_profile,
)


class TestBuildChannelResolution:
    """`_resolve_build_channel` carries the whole Tier-2 policy.

    PGO + BOLT add 30-60 min per build and a bad workload makes them
    NEGATIVE, so only a build that actually ships may resolve to `release`.
    """

    _VARS = ("HYPERCI_CHANNEL", "GITHUB_REF_TYPE", "RUST_VERSION", "CI_COMMIT_TAG")

    def _clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in self._VARS:
            monkeypatch.delenv(var, raising=False)

    def test_a_plain_push_build_is_alpha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clean(monkeypatch)
        assert _resolve_build_channel(CIConfig()) == "alpha"

    def test_the_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clean(monkeypatch)
        monkeypatch.setenv("HYPERCI_CHANNEL", "release")
        assert _resolve_build_channel(CIConfig()) == "release"

    def test_the_override_is_normalised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The workflow interpolates it, so whitespace and case arrive as typed.
        self._clean(monkeypatch)
        monkeypatch.setenv("HYPERCI_CHANNEL", "  Release  ")
        assert _resolve_build_channel(CIConfig()) == "release"

    def test_an_empty_override_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The workflow sets it to '' on a validate-only run, which must fall
        # through to the inference rather than resolve to an empty channel.
        self._clean(monkeypatch)
        monkeypatch.setenv("HYPERCI_CHANNEL", "")
        assert _resolve_build_channel(CIConfig()) == "alpha"

    def test_a_tag_ref_infers_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clean(monkeypatch)
        monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
        assert _resolve_build_channel(CIConfig()) == "release"

    @pytest.mark.parametrize("var", ["RUST_VERSION", "CI_COMMIT_TAG"])
    def test_a_tagged_build_marker_infers_release(
        self, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        self._clean(monkeypatch)
        monkeypatch.setenv(var, "1.2.3")
        assert _resolve_build_channel(CIConfig()) == "release"

    def test_the_override_beats_the_ref_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Priority, not just presence: an operator pinning alpha on a tag ref
        # must not silently get a 60-minute build.
        self._clean(monkeypatch)
        monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
        monkeypatch.setenv("HYPERCI_CHANNEL", "alpha")
        assert _resolve_build_channel(CIConfig()) == "alpha"

    def test_the_publish_channel_config_does_not_leak_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `publish.channel` says WHERE artefacts go. A project that ships to
        # `release` still gets push-event CI on every commit, which must not
        # trigger Tier 2 -- the build channel is orthogonal.
        self._clean(monkeypatch)
        config = CIConfig()
        monkeypatch.setattr(config, "get", lambda *_a, **_k: "release")
        assert _resolve_build_channel(config) == "alpha"


class TestChannelDefaults:
    """Channel-tiered defaults. Allocator is jemalloc everywhere for
    consistency; LTO tiers at beta+ for CI speed."""

    def test_alpha_uses_jemalloc_and_thin_lto(self) -> None:
        p = resolve_optimization_profile("alpha", None)
        assert p.allocator == "jemalloc"
        assert p.lto == "thin"

    def test_beta_enables_jemalloc_and_fat_lto(self) -> None:
        p = resolve_optimization_profile("beta", None)
        assert p.allocator == "jemalloc"
        assert p.lto == "fat"

    def test_release_enables_jemalloc_and_fat_lto(self) -> None:
        p = resolve_optimization_profile("release", None)
        assert p.allocator == "jemalloc"
        assert p.lto == "fat"

    def test_unknown_channel_falls_back_to_alpha_defaults(self) -> None:
        p = resolve_optimization_profile("random-channel-name", None)
        assert p.allocator == "jemalloc"
        assert p.lto == "thin"

    def test_retired_spike_channel_resolves_to_the_alpha_tier(self) -> None:
        # A repo config still naming the removed spike channel degrades to
        # the lowest tier instead of breaking the build.
        p = resolve_optimization_profile("spike", None)
        assert p.allocator == "jemalloc"
        assert p.lto == "thin"
        assert p.pgo_enabled is False
        assert p.bolt_enabled is False


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


class TestSkipOptimize:
    """issue #132: a per-run switch drops the optimisation stage.

    For Rust that is PGO + BOLT. Tier 1 (allocator + LTO) is untouched, so
    the result is a plain release build rather than an unoptimised one.
    """

    @staticmethod
    def _opted_in() -> dict:
        return {
            "pgo": {"enabled": True, "workload_cmd": "bash x.sh"},
            "bolt": {"enabled": True},
        }

    @classmethod
    def _skipped(cls, channel: str = "release"):
        return resolve_optimization_profile(
            channel, cls._opted_in(), skip_optimize=True
        )

    def test_default_keeps_pgo_and_bolt_on(self) -> None:
        # Upgrading hyperi-ci must not change any existing repo's build.
        p = resolve_optimization_profile("release", self._opted_in())
        assert p.pgo_enabled is True
        assert p.bolt_enabled is True
        assert p.optimize_skipped is False

    def test_skip_disables_pgo_and_bolt(self) -> None:
        p = self._skipped()
        assert p.pgo_enabled is False
        assert p.bolt_enabled is False

    def test_skip_keeps_tier_one(self) -> None:
        p = self._skipped()
        assert p.allocator == "jemalloc"
        assert p.lto == "fat"

    def test_skip_is_marked_even_where_tier_two_never_ran(self) -> None:
        # alpha has no PGO or BOLT to drop, and the log line still says why.
        assert self._skipped("alpha").optimize_skipped is True

    def test_describe_names_the_skip(self) -> None:
        # The run log is where an unoptimised binary announces itself.
        described = self._skipped().describe()
        assert "optimize=skipped" in described
        assert "pgo=on" not in described
        assert "bolt=on" not in described

    def test_validate_preserves_the_skip_flag(self) -> None:
        validated = validate_profile(self._skipped(), cargo_features={"jemalloc"})
        assert validated.optimize_skipped is True
        assert "optimize=skipped" in validated.describe()


class TestUnoptimizedReleaseRefusal:
    """issue #158: skipping optimisation and releasing are two consents."""

    OPTED_IN = {"pgo": {"enabled": True, "workload_cmd": "bash x.sh"}}

    def test_refuses_a_skipped_release_without_consent(self) -> None:
        msg = unoptimized_release_refusal(
            "release", self.OPTED_IN, skip_optimize=True, release_unoptimized=False
        )
        assert msg is not None
        assert "release-unoptimized" in msg, "the refusal must name the override"

    def test_consent_lets_it_through(self) -> None:
        assert (
            unoptimized_release_refusal(
                "release", self.OPTED_IN, skip_optimize=True, release_unoptimized=True
            )
            is None
        )

    def test_an_optimised_release_is_never_refused(self) -> None:
        assert (
            unoptimized_release_refusal(
                "release", self.OPTED_IN, skip_optimize=False, release_unoptimized=False
            )
            is None
        )

    @pytest.mark.parametrize("channel", ["alpha", "beta"])
    def test_prerelease_channels_may_skip_freely(self, channel: str) -> None:
        assert (
            unoptimized_release_refusal(
                channel, self.OPTED_IN, skip_optimize=True, release_unoptimized=False
            )
            is None
        )

    def test_a_release_with_no_tier_two_loses_nothing(self) -> None:
        # Nothing to skip, so nothing to consent to.
        assert (
            unoptimized_release_refusal(
                "release", {}, skip_optimize=True, release_unoptimized=False
            )
            is None
        )


class TestConventionalWorkloadDefault:
    """issue #143: a release profiles itself when the project ships a workload."""

    @staticmethod
    def _with_script(root: Path) -> Path:
        script = root / optimize.CONVENTIONAL_WORKLOAD_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")
        return script

    def test_no_workload_means_no_pgo(self, tmp_path: Path) -> None:
        profile = resolve_optimization_profile("release", None, project_root=tmp_path)
        assert profile.pgo_enabled is False
        assert profile.bolt_enabled is False

    def test_the_conventional_script_turns_it_on(self, tmp_path: Path) -> None:
        self._with_script(tmp_path)
        profile = resolve_optimization_profile("release", None, project_root=tmp_path)
        assert profile.pgo_enabled is True
        assert profile.bolt_enabled is True
        assert profile.pgo_workload_cmd == optimize.CONVENTIONAL_WORKLOAD_CMD

    def test_an_explicit_opt_out_still_wins(self, tmp_path: Path) -> None:
        self._with_script(tmp_path)
        profile = resolve_optimization_profile(
            "release", {"pgo": {"enabled": False}}, project_root=tmp_path
        )
        assert profile.pgo_enabled is False

    def test_bolt_can_be_declined_on_its_own(self, tmp_path: Path) -> None:
        self._with_script(tmp_path)
        profile = resolve_optimization_profile(
            "release", {"bolt": {"enabled": False}}, project_root=tmp_path
        )
        assert profile.pgo_enabled is True
        assert profile.bolt_enabled is False

    @pytest.mark.parametrize("channel", ["alpha", "beta"])
    def test_a_prerelease_never_profiles(self, channel: str, tmp_path: Path) -> None:
        self._with_script(tmp_path)
        profile = resolve_optimization_profile(channel, None, project_root=tmp_path)
        assert profile.pgo_enabled is False

    def test_no_root_does_no_lookup(self, tmp_path: Path) -> None:
        # The caller with no tree on disk keeps the explicit-opt-in behaviour,
        # which is what every existing caller and test relies on.
        self._with_script(tmp_path)
        profile = resolve_optimization_profile("release", None)
        assert profile.pgo_enabled is False

    def test_skip_optimize_still_beats_the_default(self, tmp_path: Path) -> None:
        self._with_script(tmp_path)
        profile = resolve_optimization_profile(
            "release", None, skip_optimize=True, project_root=tmp_path
        )
        assert profile.pgo_enabled is False


class TestRefusalSeesTheDefault:
    """issue #143 + #158: a defaulted project must not lose its consent gate."""

    def test_a_defaulted_project_is_refused(self, tmp_path: Path) -> None:
        # Without the root the refusal resolves pgo off and waves the build
        # through, which is how a skipped release would ship unannounced once
        # the per-project stanzas go.
        script = tmp_path / optimize.CONVENTIONAL_WORKLOAD_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")
        msg = unoptimized_release_refusal(
            "release",
            None,
            skip_optimize=True,
            release_unoptimized=False,
            project_root=tmp_path,
        )
        assert msg is not None
        assert "release-unoptimized" in msg

    def test_a_project_with_no_workload_is_not_refused(self, tmp_path: Path) -> None:
        assert (
            unoptimized_release_refusal(
                "release",
                None,
                skip_optimize=True,
                release_unoptimized=False,
                project_root=tmp_path,
            )
            is None
        )


class TestCargoFeatures:
    """`cargo_features()` returns the feature list for `--features` flag."""

    def test_system_allocator_returns_empty_list(self) -> None:
        p = OptimizationProfile(channel="alpha", allocator="system")
        assert p.cargo_features() == []

    def test_jemalloc_returns_jemalloc_feature(self) -> None:
        p = OptimizationProfile(channel="release", allocator="jemalloc")
        assert p.cargo_features() == ["jemalloc"]

    def test_mimalloc_returns_mimalloc_feature(self) -> None:
        p = OptimizationProfile(channel="release", allocator="mimalloc")
        assert p.cargo_features() == ["mimalloc"]


class TestCargoFeatureArgs:
    """`cargo_feature_args()` is the one renderer every cargo line uses.

    The allocator is ADDED to what `build.rust.features` declares -- a
    release image that drops the declared features refuses at load every
    engine it was configured with (#130).
    """

    _RELEASE = OptimizationProfile(channel="release", allocator="jemalloc")

    def test_declared_features_keep_the_allocator(self) -> None:
        args = cargo_feature_args(self._RELEASE, "db-clickhouse,file-tail")
        assert args == ["--features", "db-clickhouse,file-tail,jemalloc"]

    def test_pipe_joined_list_becomes_one_comma_separated_set(self) -> None:
        """The dispatcher joins a YAML features list with a pipe character."""
        args = cargo_feature_args(
            self._RELEASE, "jemalloc|db-clickhouse|db-mongodb|file-tail"
        )
        assert args == [
            "--features",
            "jemalloc,db-clickhouse,db-mongodb,file-tail",
        ]

    def test_allocator_is_not_repeated(self) -> None:
        args = cargo_feature_args(self._RELEASE, "jemalloc,db-clickhouse")
        assert args == ["--features", "jemalloc,db-clickhouse"]

    def test_system_allocator_passes_only_declared_features(self) -> None:
        p = OptimizationProfile(channel="release", allocator="system")
        assert cargo_feature_args(p, "db-clickhouse") == [
            "--features",
            "db-clickhouse",
        ]

    def test_no_profile_and_no_features_renders_nothing(self) -> None:
        assert cargo_feature_args(None, "") == []

    def test_sentinels_are_not_cargo_features(self) -> None:
        """The words all and default name a stage feature set, not a crate's."""
        assert cargo_feature_args(None, "all") == []
        assert cargo_feature_args(None, "default") == []
        assert cargo_feature_args(self._RELEASE, "default|db-clickhouse") == [
            "--features",
            "db-clickhouse,jemalloc",
        ]

    def test_all_features_supersedes_the_list(self) -> None:
        args = cargo_feature_args(self._RELEASE, "db-clickhouse", all_features=True)
        assert args == ["--all-features"]


class TestEnvOverrides:
    """`env_overrides()` injects CARGO_PROFILE_RELEASE_LTO at build time."""

    def test_fat_lto_sets_env_var(self) -> None:
        p = OptimizationProfile(channel="release", lto="fat")
        assert p.env_overrides() == {"CARGO_PROFILE_RELEASE_LTO": "fat"}

    def test_thin_lto_sets_env_var(self) -> None:
        p = OptimizationProfile(channel="alpha", lto="thin")
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


class TestOutcomeDescribe:
    """The Tier 2 summary reports what ran, not what was requested."""

    def test_fully_optimised(self) -> None:
        o = OptimizationOutcome(
            allocator="jemalloc", pgo_applied=True, bolt_applied=True
        )
        assert o.describe() == "optimised: pgo=yes bolt=yes allocator=jemalloc"

    def test_bolt_skipped(self) -> None:
        o = OptimizationOutcome(allocator="jemalloc", pgo_applied=True)
        assert o.describe() == "optimised: pgo=yes bolt=no allocator=jemalloc"

    def test_nothing_applied_defaults_to_system_allocator(self) -> None:
        assert (
            OptimizationOutcome().describe()
            == "optimised: pgo=no bolt=no allocator=system"
        )

    def test_log_outcome_emits_the_line_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lines: list[str] = []
        monkeypatch.setattr(optimize, "info", lines.append)

        log_outcome(OptimizationOutcome(allocator="mimalloc", pgo_applied=True))

        assert lines == ["optimised: pgo=yes bolt=no allocator=mimalloc"]
