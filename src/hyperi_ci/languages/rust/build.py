# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/build.py
# Purpose:   Rust build handler for the host's own target
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust build handler: release builds for the host's own target.

There is no cross-compilation. CI builds each target on a runner of that arch,
and a target for another arch is skipped locally and fails in CI.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hyperi_ci.common import (
    announce,
    error,
    group,
    info,
    is_ci,
    is_prerelease_build,
    optimize_tier,
    release_unoptimized,
    run_cmd,
    sanitize_ref_name,
    skip_optimize,
    success,
    truthy,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages._build_common import (
    elf_section_names,
)
from hyperi_ci.languages._build_common import (
    generate_checksums as _generate_checksums,
)
from hyperi_ci.languages._build_common import (
    human_size as _human_size,
)
from hyperi_ci.languages.rust.optimize import (
    OptimizationOutcome,
    OptimizationProfile,
    cargo_feature_args,
    log_outcome,
    log_profile,
    parse_cargo_features,
    resolve_optimization_profile,
    tier2_shortfall,
    unoptimized_release_refusal,
    validate_profile,
)
from hyperi_ci.languages.rust.pgo import (
    BOLT_NOTE_SECTION,
    bolt_optimize_args_override,
)
from hyperi_ci.languages.rust.targets import cargo_metadata

_TARGET_MAP = {
    "x86_64-unknown-linux-gnu": ("linux", "amd64"),
    "aarch64-unknown-linux-gnu": ("linux", "arm64"),
    "x86_64-apple-darwin": ("darwin", "amd64"),
    "aarch64-apple-darwin": ("darwin", "arm64"),
}


def _get_native_target() -> str:
    """Return the native Rust target triple, from the machine arch on Linux too.

    An arm64 runner answering x86_64 would read its own target as another
    arch's and refuse to build it.
    """
    import platform

    arch = platform.machine()
    if sys.platform == "darwin":
        return "aarch64-apple-darwin" if arch == "arm64" else "x86_64-apple-darwin"
    return (
        "aarch64-unknown-linux-gnu"
        if arch in ("aarch64", "arm64")
        else "x86_64-unknown-linux-gnu"
    )


_ELF_MACHINE_MAP = {
    "x86_64": "Advanced Micro Devices X86-64",
    "aarch64": "AArch64",
    "i686": "Intel 80386",
    "armv7": "ARM",
    "riscv64": "RISC-V",
}


def _target_to_elf_machine(target: str) -> str | None:
    """Map Rust target to expected ELF machine string from readelf -h."""
    for prefix, machine in _ELF_MACHINE_MAP.items():
        if target.startswith(prefix):
            return machine
    return None


def _verify_binary(binary: Path, target: str) -> bool:
    """Check a binary's size and ELF machine type, then smoke-test it."""
    errors = 0
    info("    --- Post-build verification ---")

    if not binary.exists():
        error(f"    Binary not found: {binary}")
        return False

    size = binary.stat().st_size
    if size < 102400:
        error(f"    Binary too small ({size} bytes) -- likely corrupt")
        errors += 1
    else:
        info(f"    OK: Size {_human_size(size)}")

    if shutil.which("file"):
        result = run_cmd(["file", str(binary)], check=False, capture=True)
        if "ELF" not in result.stdout:
            error(f"    Not an ELF binary: {result.stdout.strip()}")
            errors += 1
        else:
            info("    OK: ELF binary confirmed")

    if shutil.which("readelf"):
        expected = _target_to_elf_machine(target)
        if expected:
            result = run_cmd(["readelf", "-h", str(binary)], check=False, capture=True)
            for line in result.stdout.splitlines():
                if "Machine:" in line:
                    actual = line.split("Machine:")[1].strip()
                    if expected in actual:
                        info(f"    OK: Machine type: {actual}")
                    else:
                        error(
                            f"    Wrong machine type: got '{actual}', expected '{expected}'"
                        )
                        errors += 1
                    break

        result = run_cmd(["readelf", "-d", str(binary)], check=False, capture=True)
        deps = []
        for line in result.stdout.splitlines():
            if "NEEDED" in line and "[" in line:
                dep = line.split("[")[-1].rstrip("]").strip()
                if dep:
                    deps.append(dep)
        if deps:
            info(f"    OK: Dynamic deps ({len(deps)}): {' '.join(deps)}")
        else:
            info("    INFO: Statically linked (no dynamic deps)")

    for flag in ("--version", "--help"):
        try:
            result = run_cmd([str(binary), flag], check=False, capture=True, timeout=10)
            if result.returncode == 0:
                first_line = result.stdout.splitlines()[0] if result.stdout else ""
                info(f"    OK: Smoke test ({flag}): {first_line}")
                break
        except subprocess.TimeoutExpired:
            continue
    else:
        info("    SKIP: Smoke test (binary needs runtime config)")

    if errors:
        error(f"    {errors} verification failure(s)")
        return False

    info("    All checks passed")
    return True


def _strip_tool(target: str) -> str | None:
    """Return the strip binary for ``target``, or None when none is known."""
    if target.startswith(("x86_64-unknown-linux", "x86_64-apple", "aarch64-apple")):
        return "strip"
    if target.startswith("aarch64-unknown-linux"):
        return "aarch64-linux-gnu-strip"
    return None


def _ships_unstripped(binary: Path, reason: str) -> bool:
    """Report a binary that ships unstripped. Returns True if packaging may go on.

    PGO + BOLT compiles run with `strip=none`, so packaging is the only strip;
    a gap fails in CI and warns locally.
    """
    msg = f"{binary.name} ships unstripped: {reason}"
    if is_ci():
        announce(msg, "hyperi-ci binary not stripped", level="error")
        return False
    warn(f"    {msg} (a CI build fails here)")
    return True


def _strip_binary(binary: Path, target: str) -> bool:
    """Strip symbols from a packaged binary.

    Returns:
        False when the binary could not be stripped and the run is in CI,
        which fails packaging. True otherwise, a non-Linux target with no
        known strip tool included.

    """
    strip_cmd = _strip_tool(target)
    if strip_cmd is None:
        if "linux" in target:
            return _ships_unstripped(
                binary, f"no strip tool is known for target {target}"
            )
        return True
    if not shutil.which(strip_cmd):
        return _ships_unstripped(binary, f"{strip_cmd} is not on PATH")

    size_before = binary.stat().st_size
    result = run_cmd([strip_cmd, str(binary)], check=False)
    if result.returncode != 0:
        return _ships_unstripped(binary, f"{strip_cmd} exited {result.returncode}")
    size_after = binary.stat().st_size
    saved = _human_size(size_before - size_after)
    info(
        f"    Stripped: {_human_size(size_before)} -> {_human_size(size_after)} (saved {saved})"
    )
    return True


def _resolve_build_channel(config: CIConfig) -> str:
    """Resolve the build channel that gates optimisation tiers.

    Order: `HYPERCI_CHANNEL`, then `optimize-tier: release` (issue #257), then
    any other ship signal, else "alpha". `release.channel` is deliberately not
    read: Tier 2 adds 30-60 min per build and must run only on a build that
    ships, never on every push.
    """
    override = os.environ.get("HYPERCI_CHANNEL", "").strip().lower()
    if override:
        return override

    if optimize_tier() == "release":
        return "release"

    if _ship_signal():
        return "release"

    return "alpha"


def _ship_signal() -> str | None:
    """Name the env signal that says this build ships, or None when none does.

    The workflows set `HYPERCI_CHANNEL` only when the plan predicts a release;
    the tag signals cover builds outside them. `optimize-tier` is not a ship
    signal.
    """
    channel = os.environ.get("HYPERCI_CHANNEL", "").strip()
    if channel:
        return f"HYPERCI_CHANNEL={channel}"
    if os.environ.get("GITHUB_REF_TYPE", "").strip().lower() == "tag":
        return "GITHUB_REF_TYPE=tag"
    for var in ("RUST_VERSION", "CI_COMMIT_TAG"):
        if os.environ.get(var, "").strip():
            return var
    return None


def _bolt_override_refusal() -> str | None:
    """Say why this run may not take `bolt-optimize-args`, or None when it may.

    Unreviewed BOLT flags must never ship, and without `optimize-tier=release`
    the run never reaches BOLT and would test nothing.
    """
    try:
        override = bolt_optimize_args_override()
    except ValueError as exc:
        return str(exc)
    if override is None:
        return None
    signal = _ship_signal()
    if signal:
        return (
            f"bolt-optimize-args is debug-only and this run ships ({signal}). "
            "A release always builds with the reviewed BOLT flags; drop the "
            "input, or dispatch validate-only (no tag, no from-head)."
        )
    if optimize_tier() != "release":
        return (
            "bolt-optimize-args only changes the BOLT optimise step, which a "
            "validate-only run reaches with optimize-tier=release. Dispatch "
            "with both."
        )
    return None


def _detect_cargo_features() -> set[str]:
    """Union the `[features]` tables of the root manifest and every member.

    A virtual workspace root declares none, so the root alone would hide a
    member's allocator feature.
    """
    features = parse_cargo_features(Path.cwd() / "Cargo.toml")

    meta = cargo_metadata()
    if meta is None:
        return features

    for package in meta.get("packages", []):
        manifest = package.get("manifest_path")
        if manifest:
            features |= parse_cargo_features(Path(manifest))
    return features


def _detect_binary_names() -> list[str]:
    """Return the shipped binary names; empty for a library-only crate.

    Falls back to the directory name only when cargo metadata fails.
    """
    meta = cargo_metadata()
    if meta is None:
        return [Path.cwd().name]
    return binary_targets(meta)


def binary_targets(meta: dict) -> list[str]:
    """Return the unconditional binary target names in ``cargo metadata`` output."""
    names: list[str] = []
    for package in meta.get("packages", []):
        for target in package.get("targets", []):
            if "bin" not in target.get("kind", []):
                continue
            # Feature-gated bins are tools (PGO drivers, benches), and forcing
            # them in breaks the default build.
            if target.get("required-features"):
                continue
            names.append(target["name"])

    return names


def stamp_manifest(version: str, root: Path) -> None:
    """Stamp `version` into Cargo.toml's [package] and [workspace.package].

    Dependency pins are untouched. A member Cargo.toml that pins its own
    version is not stamped.
    """
    from hyperi_ci.stamp import replace_toml_table_version

    cargo = root / "Cargo.toml"
    if not cargo.exists():
        return
    text = cargo.read_text(encoding="utf-8")
    for table in ("package", "workspace.package"):
        text = replace_toml_table_version(text, table, version)
    cargo.write_text(text, encoding="utf-8", newline="\n")
    info(f"Stamped Cargo.toml: {version}")


def _detect_version() -> str:
    """Return the version from VERSION, then the env, then Cargo.toml, else "dev".

    GITHUB_REF_NAME is excluded: in the publish job it is the branch, not the tag.
    """
    version_file = Path("VERSION")
    if version_file.exists():
        val = version_file.read_text(encoding="utf-8").strip()
        if val:
            return f"v{val}" if not val.startswith("v") else val

    for var in ("RUST_VERSION", "CI_COMMIT_TAG"):
        val = os.environ.get(var, "")
        if val:
            return sanitize_ref_name(val)

    meta = cargo_metadata()
    if meta is not None:
        for package in meta.get("packages", []):
            version = package.get("version", "")
            if version:
                return f"v{version}"

    return "dev"


def windows_targets(targets: list[str]) -> list[str]:
    """Return the Windows triples in ``targets``, which the build does not support."""
    return [t for t in targets if "-windows" in t]


def _target_to_os_arch(target: str) -> str:
    """Map Rust target triple to os-arch naming (matches Go convention)."""
    pair = _TARGET_MAP.get(target)
    if pair:
        return f"{pair[0]}-{pair[1]}"
    return target


def _package_binaries(
    targets: list[str],
    binary_names: list[str],
    version: str,
) -> int:
    """Copy, strip and verify built binaries into dist/ as ``<name>-<os>-<arch>``.

    The version goes in the R2 and release path, not the filename.
    """
    output_dir = Path("dist")
    output_dir.mkdir(parents=True, exist_ok=True)
    target_dir = os.environ.get("CARGO_TARGET_DIR", "target")
    built_count = 0

    info(f"Packaging binaries: {' '.join(binary_names)}")
    info(f"Version: {version}")

    for target in targets:
        os_arch = _target_to_os_arch(target)
        profile_dir = Path(target_dir) / target / "release"

        for bin_name in binary_names:
            src_bin = profile_dir / bin_name
            output_name = f"{bin_name}-{os_arch}"

            if not src_bin.exists():
                error(f"Binary not found: {src_bin}")
                return 1

            output_path = output_dir / output_name
            shutil.copy2(src_bin, output_path)
            output_path.chmod(0o755)

            if not _strip_binary(output_path, target):
                return 1

            info(
                f"  Created: {output_path.name} ({_human_size(output_path.stat().st_size)})"
            )

            if not _verify_binary(output_path, target):
                error(f"Post-build verification failed for {output_path}")
                return 1

            built_count += 1

    info(f"Built {built_count} binary(ies) to {output_dir}/")
    for f in sorted(output_dir.iterdir()):
        if f.is_file() and f.suffix != ".sha256":
            info(f"  {f.name} ({_human_size(f.stat().st_size)})")

    _generate_checksums(output_dir)

    return 0


def _verify_bolt_shipped(
    targets: list[str], binary_name: str, output_dir: Path = Path("dist")
) -> int:
    """Check each BOLT-optimised target's packaged file is the BOLT output.

    Reads the shipped file rather than trusting cargo-pgo's log, so a
    packaging slip cannot pass as an optimised release.

    Args:
        targets: Targets whose outcome recorded BOLT as applied.
        binary_name: The binary PGO and BOLT ran on.
        output_dir: Where packaging wrote the artefacts.

    Returns:
        0 when every file carries llvm-bolt's note, 1 on the first that does not.

    """
    for target in targets:
        shipped = output_dir / f"{binary_name}-{_target_to_os_arch(target)}"
        if BOLT_NOTE_SECTION in elf_section_names(shipped):
            info(f"  BOLT verified in {shipped.name} ({BOLT_NOTE_SECTION})")
            continue
        error(
            f"{shipped} was reported BOLT-optimised but carries no "
            f"{BOLT_NOTE_SECTION}: the packaged file is not the BOLT output"
        )
        return 1
    return 0


def _build_for_target(
    target: str,
    features: str,
    all_features: bool,
    extra_env: dict[str, str] | None = None,
    profile: OptimizationProfile | None = None,
    outcome: OptimizationOutcome | None = None,
) -> int:
    """Build one host-arch target triple, through the PGO pipeline when asked.

    Both paths carry the declared features, the allocator and the LTO override.
    """
    if profile:
        profile_env = profile.env_overrides()
        extra_env = {**(extra_env or {}), **profile_env}

    if profile and profile.pgo_enabled:
        binary_names = _detect_binary_names()
        if not binary_names:
            warn(
                "PGO requested but crate has no binaries -- falling back to plain build"
            )
        else:
            # The workload profiles the first binary; each step builds all shipped ones.
            from hyperi_ci.languages.rust.pgo import run_pgo_build

            return run_pgo_build(
                target=target,
                profile=profile,
                binary_name=binary_names[0],
                shipped_binaries=binary_names,
                cwd=Path.cwd(),
                extra_env={
                    **(extra_env or {}),
                    "RUST_FEATURES": features,
                    "RUST_ALL_FEATURES": "true" if all_features else "false",
                },
                outcome=outcome,
            )

    feature_args = cargo_feature_args(profile, features, all_features=all_features)
    cmd = ["cargo", "build", "--release", "--target", target, *feature_args]

    env = dict(extra_env or {})
    info(f"  Building for {target}...")
    return run_cmd(cmd, check=False, env=env).returncode


def _host_targets(targets: list[str], native: str) -> list[str] | None:
    """Return the targets this host builds, or None when CI named another arch.

    hyperi-ci does not cross-compile: each target builds on a runner of its own
    arch, one leg per arch in rust-ci.yml's build matrix. A foreign target in CI
    is a leg on the wrong runner and fails the build. Elsewhere it is skipped
    with a warning.
    """
    foreign = [t for t in targets if t != native]
    if not foreign:
        return targets
    names = ", ".join(foreign)
    if is_ci():
        error(
            f"Build target {names} is not this runner's arch ({native}). hyperi-ci "
            "does not cross-compile: run the leg on a runner of that arch "
            "(GH_RUNNER_ARM64 for aarch64-unknown-linux-gnu)."
        )
        return None
    warn(
        f"Skipping {names}: not this host's arch ({native}), and hyperi-ci does "
        "not cross-compile. CI builds each target on a runner of its own arch."
    )
    return [t for t in targets if t == native]


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Rust build.

    Args:
        config: Merged CI configuration.
        extra_env: Additional env vars (RUST_BUILD_TARGETS, RUST_FEATURES, etc).

    Returns:
        Exit code (0 = success).

    """
    if not shutil.which("cargo"):
        error("cargo not installed")
        return 1

    extra = extra_env or {}
    info("Building Rust project...")

    bolt_refusal = _bolt_override_refusal()
    if bolt_refusal:
        announce(bolt_refusal, "hyperi-ci bolt-optimize-args refused", level="error")
        return 1
    bolt_override = bolt_optimize_args_override()
    if bolt_override:
        announce(
            "BOLT optimise flags overridden for this run (debug only, refused on "
            f"any run that ships): {' '.join(bolt_override)}.",
            "hyperi-ci BOLT flags overridden",
        )

    features = extra.get("RUST_FEATURES", "")
    all_features = extra.get("RUST_ALL_FEATURES", "false") == "true"
    targets_str = extra.get("RUST_BUILD_TARGETS", "")

    if targets_str:
        targets = [t.strip() for t in targets_str.split(",") if t.strip()]
    else:
        targets = [_get_native_target()]
    if unsupported := windows_targets(targets):
        error(
            f"Windows targets are not supported: {', '.join(unsupported)}. "
            "Remove them from build.rust.targets."
        )
        return 1

    host_targets = _host_targets(targets, _get_native_target())
    if host_targets is None:
        return 1
    targets = host_targets

    # A library gets no profile: consumers recompile it from source.
    binary_names_for_profile = _detect_binary_names()
    base_profile: OptimizationProfile | None = None
    if binary_names_for_profile:
        tier = optimize_tier()
        if tier and tier != "release":
            refusal = (
                f"optimize-tier={tier!r} is not a tier. The one value is "
                "'release' (PGO + BOLT on a run that publishes nothing); leave "
                "it empty otherwise."
            )
            announce(refusal, "hyperi-ci optimize-tier refused", level="error")
            return 1
        channel = _resolve_build_channel(config)
        user_optimize = config.get("build.rust.optimize") or {}
        skip = skip_optimize(config)
        consented = release_unoptimized()
        project_root = Path.cwd()
        prerelease = is_prerelease_build()
        refusal = unoptimized_release_refusal(
            channel,
            user_optimize,
            skip_optimize=skip,
            release_unoptimized=consented,
            project_root=project_root,
            prerelease=prerelease,
        )
        if refusal:
            announce(refusal, "hyperi-ci unoptimised release refused", level="error")
            return 1
        if skip and channel == "release" and consented:
            msg = (
                "Shipping a release with the optimisation stage skipped, by "
                "explicit consent (release-unoptimized=true)."
            )
            announce(msg, "hyperi-ci unoptimised release")
        if skip:
            # An unoptimised binary looks identical until someone benchmarks it.
            msg = (
                "Optimisation stage skipped for this build -- no PGO, no BOLT. "
                "Tier 1 (allocator + LTO) still applies. Unset "
                "HYPERCI_SKIP_OPTIMIZE / build.skip_optimize for a fully "
                "optimised binary."
            )
            announce(msg, "hyperi-ci optimisation skipped")
        base_profile = resolve_optimization_profile(
            channel, user_optimize, skip_optimize=skip, project_root=project_root
        )
        cargo_features = _detect_cargo_features()

    tier2 = bool(
        base_profile and (base_profile.pgo_enabled or base_profile.bolt_enabled)
    )

    # A skipped Tier 2 stage fails the release rather than shipping it green.
    strict = truthy(config.get("build.rust.optimize.strict", True))
    bolt_targets: list[str] = []
    for target in targets:
        with group(f"Build: {target}"):
            profile = None
            outcome = None
            if base_profile:
                profile = validate_profile(
                    base_profile,
                    cargo_features=cargo_features,
                    target=target,
                )
                log_profile(profile)
                outcome = OptimizationOutcome(allocator=profile.allocator)
            rc = _build_for_target(
                target, features, all_features, extra, profile=profile, outcome=outcome
            )
            if rc != 0:
                error(f"Build failed for target: {target}")
                return rc
            if tier2 and outcome:
                log_outcome(outcome)
            if profile and outcome and profile.channel == "release" and strict:
                missing = tier2_shortfall(profile, outcome)
                if missing:
                    error(
                        f"{target}: {' and '.join(missing)} was requested for this "
                        "release and did not reach the binary. Refusing to ship a "
                        "half-optimised release -- the warnings above name the cause. "
                        "Set build.rust.optimize.strict: false to ship it anyway."
                    )
                    return 1
            if outcome and outcome.bolt_applied:
                bolt_targets.append(target)
            success(f"Built: {target}")

    with group("Binary packaging"):
        binary_names = _detect_binary_names()
        if not binary_names:
            info("Library-only crate -- skipping binary packaging")
        else:
            version = _detect_version()
            rc = _package_binaries(targets, binary_names, version)
            if rc != 0:
                return rc
            rc = _verify_bolt_shipped(bolt_targets, binary_names[0])
            if rc != 0:
                return rc

    success("Build complete")
    return 0
