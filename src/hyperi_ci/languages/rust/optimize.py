# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/optimize.py
# Purpose:   Channel-gated release-track build optimisation profile
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release-track build optimisation profile.

Resolves the optimisation profile for a Rust build based on the project's
publish channel and user config. Channel gating:

    alpha   -> jemalloc allocator + thin LTO (fast feedback cycles)
    beta    -> jemalloc allocator + fat LTO
    release -> jemalloc + fat LTO + optional PGO + optional BOLT

User config in `.hyperi-ci.yaml` under `build.rust.optimize` overrides
the channel defaults. Each key is optional; omitted keys use the default
for the channel.

Library-only crates skip this whole path — consumers choose their own
build profile when compiling from crates.io source.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from hyperi_ci.common import info, warn

# jemalloc at every channel, so an alpha binary profiles like a release one.
# Fat LTO costs 5-10 min per CI run, so it starts at beta.
_CHANNEL_DEFAULTS: dict[str, dict[str, str]] = {
    "alpha": {"allocator": "jemalloc", "lto": "thin"},
    "beta": {"allocator": "jemalloc", "lto": "fat"},
    "release": {"allocator": "jemalloc", "lto": "fat"},
}


@dataclass(frozen=True)
class OptimizationProfile:
    """Resolved build optimisation settings for a single CI build.

    Channel-gated with user overrides applied. Library crates should
    never have one of these — check `_detect_binary_names()` first and
    skip if empty.
    """

    channel: str
    allocator: str = "system"  # "system" | "jemalloc" | "mimalloc"
    lto: str = "thin"  # "thin" | "fat"
    pgo_enabled: bool = False
    pgo_workload_cmd: str | None = None
    pgo_workload_setup_cmd: str | None = None
    pgo_duration_secs: int = 300
    bolt_enabled: bool = False
    optimize_skipped: bool = False
    warnings: list[str] = field(default_factory=list)

    def cargo_features(self) -> list[str]:
        """Allocator features to pass via --features. Empty if system."""
        if self.allocator in ("", "system"):
            return []
        return [self.allocator]

    def env_overrides(self) -> dict[str, str]:
        """Env vars to inject at build time.

        `CARGO_PROFILE_RELEASE_LTO` overrides the Cargo.toml
        `[profile.release].lto` setting at build time without touching
        the source tree. Keeps local `cargo build` behaviour intact.
        """
        return {"CARGO_PROFILE_RELEASE_LTO": self.lto}

    def describe(self) -> str:
        """Human-readable one-line summary for CI logs."""
        parts = [
            f"channel={self.channel}",
            f"allocator={self.allocator}",
            f"lto={self.lto}",
        ]
        if self.pgo_enabled:
            parts.append("pgo=on")
        if self.bolt_enabled:
            parts.append("bolt=on")
        if self.optimize_skipped:
            parts.append("optimize=skipped")
        return ", ".join(parts)


@dataclass
class OptimizationOutcome:
    """What the Tier 2 pipeline actually did for one target.

    `OptimizationProfile.describe()` reports the request; this reports the
    result. Every Tier 2 skip is warn-only, so without this line a log
    reader cannot tell an optimised arch from one that quietly fell back.
    Mutable: the pipeline fills it in as each stage completes.
    """

    allocator: str = "system"
    pgo_applied: bool = False
    bolt_applied: bool = False

    def describe(self) -> str:
        """Human-readable one-line summary for CI logs."""
        pgo = "yes" if self.pgo_applied else "no"
        bolt = "yes" if self.bolt_applied else "no"
        return f"optimised: pgo={pgo} bolt={bolt} allocator={self.allocator}"


# "all" and "default" are stage sentinels the dispatcher passes through, not
# cargo feature names: "all" arrives as RUST_ALL_FEATURES and cargo applies the
# default set unless --no-default-features.
_FEATURE_SENTINELS = frozenset({"", "all", "default"})


def cargo_feature_args(
    profile: OptimizationProfile | None,
    features: str,
    *,
    all_features: bool = False,
) -> list[str]:
    """Render the cargo feature flags for one build of one target.

    Every cargo line a target's build issues -- plain release, PGO
    instrument, PGO optimise, BOLT -- renders its features here, so an
    optimised binary carries the features the project declared and not
    just the allocator.

    Args:
        profile: Resolved optimisation profile, or None to add no
                 allocator feature.
        features: `build.rust.features` as the dispatcher encodes it --
                  one string separated by "," or "|", or a sentinel.
        all_features: True for `--all-features`, which already covers the
                      allocator feature.

    Returns:
        Flags to append to a cargo command line, empty when the build
        selects no features.

    """
    if all_features:
        return ["--all-features"]

    # A YAML list reaches us "|"-joined, a single entry ","-separated.
    declared = features.strip().replace("|", ",").split(",")
    allocator = profile.cargo_features() if profile else []

    merged: list[str] = []
    for feature in (*declared, *allocator):
        name = feature.strip()
        if name not in _FEATURE_SENTINELS and name not in merged:
            merged.append(name)

    return ["--features", ",".join(merged)] if merged else []


def resolve_optimization_profile(
    channel: str,
    user_optimize: dict[str, Any] | None,
    *,
    skip_optimize: bool = False,
) -> OptimizationProfile:
    """Resolve an optimisation profile from channel + user config.

    Priority: explicit user value > channel default. The `alpha` channel
    never enables PGO or BOLT by default -- a user can still opt in
    explicitly via the `optimize:` config.

    Args:
        channel: Publish channel (alpha/beta/release). An unrecognised
                 channel resolves to the alpha tier.
        user_optimize: Dict from `build.rust.optimize` in .hyperi-ci.yaml,
                       or None/empty if not configured.
        skip_optimize: Drop the optimisation stage for this run. For Rust
                       that means no PGO and no BOLT; Tier 1 (allocator
                       and LTO) still applies, so the result is a plain
                       release build. Resolved by
                       `hyperi_ci.common.skip_optimize`.

    Returns:
        Resolved `OptimizationProfile`. Never raises.

    """
    # An unrecognised channel, such as a legacy `spike` still in a repo
    # config, resolves to the lowest tier rather than failing the build.
    defaults = _CHANNEL_DEFAULTS.get(channel, _CHANNEL_DEFAULTS["alpha"])
    user = user_optimize or {}

    allocator = _normalise_allocator(user.get("allocator") or defaults["allocator"])
    lto = _normalise_lto(user.get("lto") or defaults["lto"])

    pgo_cfg = user.get("pgo") or {}
    pgo_enabled = (
        bool(pgo_cfg.get("enabled", False))
        and channel == "release"
        and not skip_optimize
    )

    bolt_cfg = user.get("bolt") or {}
    bolt_enabled = (
        bool(bolt_cfg.get("enabled", False)) and pgo_enabled and channel == "release"
    )

    return OptimizationProfile(
        channel=channel,
        allocator=allocator,
        lto=lto,
        pgo_enabled=pgo_enabled,
        pgo_workload_cmd=pgo_cfg.get("workload_cmd") or None,
        pgo_workload_setup_cmd=pgo_cfg.get("workload_setup_cmd") or None,
        pgo_duration_secs=int(pgo_cfg.get("duration_secs", 300)),
        bolt_enabled=bolt_enabled,
        optimize_skipped=skip_optimize,
    )


RELEASE_UNOPTIMIZED_INPUT = "release-unoptimized"


def unoptimized_release_refusal(
    channel: str,
    user_optimize: dict[str, Any] | None,
    *,
    skip_optimize: bool,
    release_unoptimized: bool,
) -> str | None:
    """Say why a skipped-optimisation build may not ship on the release channel.

    Skipping only costs something when the release would otherwise have run
    PGO or BOLT, so a project with no Tier 2 configured is never refused.

    Args:
        channel: Resolved build channel.
        user_optimize: `build.rust.optimize` from .hyperi-ci.yaml.
        skip_optimize: Whether this run skips the optimisation stage.
        release_unoptimized: Whether this run carries the explicit consent.

    Returns:
        The refusal message, or None when the build may go ahead.

    """
    if not skip_optimize or channel != "release" or release_unoptimized:
        return None
    full = resolve_optimization_profile(channel, user_optimize)
    if not (full.pgo_enabled or full.bolt_enabled):
        return None
    return (
        "Refusing to build a release with the optimisation stage skipped: this "
        "project's release runs PGO/BOLT and the build would ship without them "
        "under a release tag. Re-run with the "
        f"'{RELEASE_UNOPTIMIZED_INPUT}: true' dispatch input "
        "(HYPERCI_RELEASE_UNOPTIMIZED=true) to ship it anyway, or drop "
        "skip-optimize for a fully optimised release."
    )


def validate_profile(
    profile: OptimizationProfile,
    cargo_features: set[str],
    target: str | None = None,
) -> OptimizationProfile:
    """Validate a profile against the project's Cargo.toml + build target.

    Applies graceful fallbacks:
      - Allocator feature missing → warn, fall back to system.
      - PGO enabled but no workload_cmd → disable PGO (config error).
      - BOLT on non-Linux target → silent disable.

    Never raises. Returns a possibly-modified profile with warnings
    attached.

    Args:
        profile: The profile to validate.
        cargo_features: Set of feature names declared in Cargo.toml's
                        `[features]` section.
        target: Build target triple (e.g. "x86_64-unknown-linux-gnu").
                None means native target (treated as host OS).

    Returns:
        A new `OptimizationProfile` with fallbacks applied.

    """
    warnings: list[str] = []
    allocator = profile.allocator
    pgo_enabled = profile.pgo_enabled
    bolt_enabled = profile.bolt_enabled

    # Allocator feature presence check
    if allocator in ("jemalloc", "mimalloc") and allocator not in cargo_features:
        warnings.append(
            f"allocator '{allocator}' requested but feature not declared in "
            f"Cargo.toml — falling back to system allocator"
        )
        allocator = "system"

    # PGO needs a workload_cmd
    if pgo_enabled and not profile.pgo_workload_cmd:
        warnings.append(
            "pgo.enabled=true but no workload_cmd configured — disabling PGO"
        )
        pgo_enabled = False
        bolt_enabled = False  # BOLT needs PGO

    # BOLT is Linux-only (ELF + llvm-bolt)
    if bolt_enabled and target and not _is_linux_target(target):
        warnings.append(
            f"BOLT requested but target '{target}' is not Linux — skipping BOLT"
        )
        bolt_enabled = False

    for w in warnings:
        warn(w)

    # Keep existing warnings from prior validation passes
    combined = list(profile.warnings) + warnings

    return replace(
        profile,
        allocator=allocator,
        pgo_enabled=pgo_enabled,
        bolt_enabled=bolt_enabled,
        warnings=combined,
    )


def log_profile(profile: OptimizationProfile) -> None:
    """Emit an INFO line describing the profile (for CI log visibility)."""
    info(f"Rust build optimisation: {profile.describe()}")


def log_outcome(outcome: OptimizationOutcome) -> None:
    """Emit an INFO line describing what the Tier 2 pipeline actually ran."""
    info(outcome.describe())


def parse_cargo_features(cargo_toml_path: Path) -> set[str]:
    """Parse feature names from the `[features]` section of a Cargo.toml.

    Returns the set of feature keys. Does NOT resolve feature unions —
    just the top-level feature names. Used for the "is 'jemalloc'
    declared?" check in validate_profile().

    Args:
        cargo_toml_path: Path to the Cargo.toml to parse.

    Returns:
        Set of feature names. Empty set if file missing or unreadable
        or no [features] section.

    """
    try:
        text = cargo_toml_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return set()

    return _parse_features_from_text(text)


def _parse_features_from_text(text: str) -> set[str]:
    """Extract feature keys from a Cargo.toml text blob.

    Stdlib-only TOML parse for the `[features]` table. We could use
    tomllib but this keeps the logic self-contained and dead simple —
    we only need the left-hand-side keys, not the feature-union arrays.
    """
    features: set[str] = set()
    in_features = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            # Entering or leaving [features] section
            in_features = line == "[features]"
            continue
        if not in_features:
            continue
        # Line looks like: key = [...]  or  key = "..."
        if "=" in line:
            key = line.split("=", 1)[0].strip()
            if key:
                features.add(key)

    return features


def _normalise_allocator(value: str | None) -> str:
    """Map None/empty/unknown to system, keep jemalloc/mimalloc as-is."""
    if not value or value == "null":
        return "system"
    v = value.strip().lower()
    if v in ("system", "jemalloc", "mimalloc"):
        return v
    return "system"


def _normalise_lto(value: str | None) -> str:
    """Map None/empty/unknown to thin, keep thin/fat as-is."""
    if not value or value == "null":
        return "thin"
    v = value.strip().lower()
    if v in ("thin", "fat"):
        return v
    return "thin"


def _is_linux_target(target: str) -> bool:
    """Check if a target triple is Linux (BOLT runs on ELF only)."""
    return "-linux-" in target
