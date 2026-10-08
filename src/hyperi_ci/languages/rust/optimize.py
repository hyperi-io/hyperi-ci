# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/optimize.py
# Purpose:   Channel-gated release-track build optimisation profile
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release-track build optimisation profile, gated by channel.

    alpha   -> jemalloc allocator + thin LTO
    beta    -> jemalloc allocator + fat LTO
    release -> jemalloc + fat LTO + optional PGO + optional BOLT

The channel here is an optimisation TIER, independent of the version: a
`1.2.0-beta.1` builds at the release tier by default, which rehearses PGO and
BOLT without spending a stable version (issue #144). `build.rust.optimize`
overrides the channel defaults key by key. Library-only crates skip this path.
"""

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
    """Resolved build optimisation settings for one CI build; never for a library."""

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
        """Return build env vars; the LTO override leaves Cargo.toml untouched."""
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
    """What the Tier 2 pipeline actually did for one target, filled in as it runs.

    Every Tier 2 skip is warn-only, so this is how a log shows whether an arch
    was optimised or quietly fell back.
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

    Every cargo build line uses this, so an optimised binary carries the
    declared features and not only the allocator.

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


# Every Rust app on the fleet that profiles keeps its workload at this path, so
# a release can find one without the project naming it.
CONVENTIONAL_WORKLOAD_SCRIPT = "scripts/pgo-workload.sh"
CONVENTIONAL_WORKLOAD_CMD = f"bash {CONVENTIONAL_WORKLOAD_SCRIPT}"


def conventional_workload_cmd(project_root: Path | None) -> str | None:
    """Return the default PGO workload command, or None with no script or root.

    Args:
        project_root: Project root to look in; None skips the lookup.

    Returns:
        The command to run, or None.

    """
    if project_root is None:
        return None
    script = project_root / CONVENTIONAL_WORKLOAD_SCRIPT
    return CONVENTIONAL_WORKLOAD_CMD if script.is_file() else None


def resolve_optimization_profile(
    channel: str,
    user_optimize: dict[str, Any] | None,
    *,
    skip_optimize: bool = False,
    project_root: Path | None = None,
) -> OptimizationProfile:
    """Resolve an optimisation profile from channel + user config.

    A user value beats the channel default. PGO and BOLT only ever run on the
    release channel.

    Args:
        channel: Publish channel (alpha/beta/release). An unrecognised one,
                 such as a legacy `spike`, resolves to the alpha tier.
        user_optimize: `build.rust.optimize` from .hyperi-ci.yaml, or None.
        skip_optimize: Drop PGO and BOLT for this run; allocator and LTO
                       still apply.
        project_root: Where to look for the conventional workload when the
                      config names none; None skips the lookup.

    Returns:
        Resolved `OptimizationProfile`. Never raises.

    """
    defaults = _CHANNEL_DEFAULTS.get(channel, _CHANNEL_DEFAULTS["alpha"])
    user = user_optimize or {}

    allocator = _normalise_allocator(user.get("allocator") or defaults["allocator"])
    lto = _normalise_lto(user.get("lto") or defaults["lto"])

    pgo_cfg = user.get("pgo") or {}
    workload_cmd = pgo_cfg.get("workload_cmd") or conventional_workload_cmd(
        project_root
    )
    # Defaults on only when a workload exists, so a project with none never fails.
    pgo_enabled = (
        bool(pgo_cfg.get("enabled", workload_cmd is not None))
        and channel == "release"
        and not skip_optimize
    )

    bolt_cfg = user.get("bolt") or {}
    bolt_enabled = (
        bool(bolt_cfg.get("enabled", True)) and pgo_enabled and channel == "release"
    )

    return OptimizationProfile(
        channel=channel,
        allocator=allocator,
        lto=lto,
        pgo_enabled=pgo_enabled,
        pgo_workload_cmd=workload_cmd,
        pgo_workload_setup_cmd=pgo_cfg.get("workload_setup_cmd") or None,
        pgo_duration_secs=int(pgo_cfg.get("duration_secs", 300)),
        bolt_enabled=bolt_enabled,
        optimize_skipped=skip_optimize,
    )


def tier2_shortfall(
    profile: OptimizationProfile, outcome: OptimizationOutcome
) -> list[str]:
    """Name the Tier 2 stages ``profile`` asked for that did not reach the binary.

    Compare against the profile after `validate_profile`, which already drops
    stages a target cannot run (BOLT off Linux), so only a real skip counts.
    """
    missing: list[str] = []
    if profile.pgo_enabled and not outcome.pgo_applied:
        missing.append("PGO")
    if profile.bolt_enabled and not outcome.bolt_applied:
        missing.append("BOLT")
    return missing


RELEASE_UNOPTIMIZED_INPUT = "release-unoptimized"


def unoptimized_release_refusal(
    channel: str,
    user_optimize: dict[str, Any] | None,
    *,
    skip_optimize: bool,
    release_unoptimized: bool,
    project_root: Path | None = None,
    prerelease: bool = False,
) -> str | None:
    """Say why a skipped-optimisation build may not ship as a stable release.

    Never refuses a project that would run neither PGO nor BOLT, nor a
    prerelease: the consent protects the stable version users install, and a
    prerelease spends none (issue #144).

    Args:
        channel: Resolved build channel -- the optimisation tier.
        user_optimize: `build.rust.optimize` from .hyperi-ci.yaml.
        skip_optimize: Whether this run skips the optimisation stage.
        release_unoptimized: Whether this run carries the explicit consent.
        project_root: Project root; without it a project relying on the
                      conventional workload reads as having nothing to skip.
        prerelease: Whether the version has a prerelease component.

    Returns:
        The refusal message, or None when the build may go ahead.

    """
    if not skip_optimize or channel != "release" or release_unoptimized:
        return None
    if prerelease:
        return None
    full = resolve_optimization_profile(
        channel, user_optimize, project_root=project_root
    )
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

    Each fallback warns: an undeclared allocator feature drops to system, PGO
    with no workload_cmd is disabled along with BOLT, and BOLT is disabled on
    a non-Linux target. Never raises.

    Args:
        profile: The profile to validate.
        cargo_features: Feature names from Cargo.toml's `[features]`.
        target: Build target triple; None means the native target.

    Returns:
        A new `OptimizationProfile` with fallbacks applied.

    """
    warnings: list[str] = []
    allocator = profile.allocator
    pgo_enabled = profile.pgo_enabled
    bolt_enabled = profile.bolt_enabled

    if allocator in ("jemalloc", "mimalloc") and allocator not in cargo_features:
        warnings.append(
            f"allocator '{allocator}' requested but feature not declared in "
            f"Cargo.toml -- falling back to system allocator"
        )
        allocator = "system"

    if pgo_enabled and not profile.pgo_workload_cmd:
        warnings.append(
            "pgo.enabled=true but no workload_cmd configured -- disabling PGO"
        )
        pgo_enabled = False
        bolt_enabled = False  # BOLT needs PGO

    # BOLT is Linux-only (ELF + llvm-bolt)
    if bolt_enabled and target and not _is_linux_target(target):
        warnings.append(
            f"BOLT requested but target '{target}' is not Linux -- skipping BOLT"
        )
        bolt_enabled = False

    for w in warnings:
        warn(w)

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
    """Return the top-level `[features]` keys of a Cargo.toml, unresolved.

    Args:
        cargo_toml_path: Path to the Cargo.toml to parse.

    Returns:
        Feature names; empty when the file is unreadable or has none.

    """
    try:
        text = cargo_toml_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return set()

    return _parse_features_from_text(text)


def _parse_features_from_text(text: str) -> set[str]:
    """Return the keys of the `[features]` table in Cargo.toml text."""
    features: set[str] = set()
    in_features = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_features = line == "[features]"
            continue
        if not in_features:
            continue
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
