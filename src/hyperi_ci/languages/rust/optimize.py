# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/optimize.py
# Purpose:   Channel-gated release-track build optimisation profile
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release-track build optimisation profile.

Resolves the optimisation profile for a Rust build based on the project's
publish channel and user config. Channel gating:

    spike / alpha -> no optimisations (fast feedback cycles)
    beta          -> jemalloc allocator + fat LTO
    release       -> jemalloc + fat LTO + optional PGO + optional BOLT

User config in `.hyperi-ci.yaml` under `build.rust.optimize` overrides
the channel defaults. Each key is optional; omitted keys use the default
for the channel.

Library-only crates skip this whole path — consumers choose their own
build profile when compiling from crates.io source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hyperi_ci.common import info, warn

# Allocator + LTO defaults per channel.
_CHANNEL_DEFAULTS: dict[str, dict[str, str]] = {
    "spike": {"allocator": "system", "lto": "thin"},
    "alpha": {"allocator": "system", "lto": "thin"},
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
    pgo_duration_secs: int = 300
    bolt_enabled: bool = False
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
        return ", ".join(parts)


def resolve_optimization_profile(
    channel: str,
    user_optimize: dict[str, Any] | None,
) -> OptimizationProfile:
    """Resolve an optimisation profile from channel + user config.

    Priority: explicit user value > channel default. `spike` / `alpha`
    channels never enable optimisations by default; users can still opt
    in explicitly via the `optimize:` config.

    Args:
        channel: Publish channel (spike/alpha/beta/release). Unknown
                 channels are treated as spike (safest default).
        user_optimize: Dict from `build.rust.optimize` in .hyperi-ci.yaml,
                       or None/empty if not configured.

    Returns:
        Resolved `OptimizationProfile`. Never raises.
    """
    defaults = _CHANNEL_DEFAULTS.get(channel, _CHANNEL_DEFAULTS["spike"])
    user = user_optimize or {}

    allocator = _normalise_allocator(user.get("allocator") or defaults["allocator"])
    lto = _normalise_lto(user.get("lto") or defaults["lto"])

    pgo_cfg = user.get("pgo") or {}
    pgo_enabled = bool(pgo_cfg.get("enabled", False)) and channel == "release"

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
        pgo_duration_secs=int(pgo_cfg.get("duration_secs", 300)),
        bolt_enabled=bolt_enabled,
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

    return OptimizationProfile(
        channel=profile.channel,
        allocator=allocator,
        lto=profile.lto,
        pgo_enabled=pgo_enabled,
        pgo_workload_cmd=profile.pgo_workload_cmd,
        pgo_duration_secs=profile.pgo_duration_secs,
        bolt_enabled=bolt_enabled,
        warnings=combined,
    )


def log_profile(profile: OptimizationProfile) -> None:
    """Emit an INFO line describing the profile (for CI log visibility)."""
    info(f"Rust build optimisation: {profile.describe()}")


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
