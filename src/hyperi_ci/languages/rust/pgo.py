# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/pgo.py
# Purpose:   PGO + BOLT build orchestration for Rust binaries
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""PGO + BOLT build orchestration.

Separate module from build.py because the PGO pipeline has a distinct
control flow (instrument -> workload -> optimise, optionally repeated
for BOLT) that's tested independently with mocked subprocesses.

Public API: `run_pgo_build()` is the only entry point. Call it when
`profile.pgo_enabled` is True; otherwise use the plain build path.

Graceful degradation:
  - cargo-pgo missing → auto-install; if install fails, skip PGO
  - llvm-bolt missing → skip BOLT, keep PGO-only result
  - workload_cmd fails → hard error (bad profile data is worse than no PGO)
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from hyperi_ci.common import error, info, run_cmd, warn
from hyperi_ci.languages._build_common import elf_section_names
from hyperi_ci.languages.rust.optimize import (
    OptimizationOutcome,
    OptimizationProfile,
    cargo_feature_args,
)
from hyperi_ci.versions import tool_version

# llvm-bolt writes this note into every binary it rewrites, and strip keeps it,
# so it survives packaging and marks a shipped file as BOLT output.
BOLT_NOTE_SECTION = ".note.bolt_info"


def run_pgo_build(
    target: str,
    profile: OptimizationProfile,
    binary_name: str,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    outcome: OptimizationOutcome | None = None,
) -> int:
    """Run the PGO (and optionally BOLT) pipeline for one target.

    Assumes `profile.pgo_enabled` is True. Caller should check first.

    Args:
        target: Target triple (e.g. "x86_64-unknown-linux-gnu").
        profile: Resolved + validated optimisation profile.
        binary_name: Name of the binary being built (for finding the
                     instrumented binary after `cargo pgo build`).
        cwd: Working directory (project root).
        extra_env: Additional env vars merged into the cargo/workload env.
                   RUST_FEATURES / RUST_ALL_FEATURES also carry the
                   project's declared `build.rust.features`.
        outcome: Filled in with the stages that actually completed, so the
                 caller can report a skip that this function warns about
                 but does not fail on.

    Returns:
        0 on success, non-zero on failure.

    """
    # Same renderer as the plain release build, so an optimised binary
    # ships the declared features and not only the allocator.
    env_features = extra_env or {}
    feature_args = cargo_feature_args(
        profile,
        env_features.get("RUST_FEATURES", ""),
        all_features=env_features.get("RUST_ALL_FEATURES") == "true",
    )

    if not _ensure_cargo_pgo_installed():
        warn(
            "cargo-pgo unavailable — falling back to plain release build "
            "(Tier 1 optimisations still apply)"
        )
        return _run_plain_release_build(target, feature_args, cwd, extra_env)

    if not _ensure_ld_lld_available():
        warn(
            "no ld.lld on PATH -- a project selecting -fuse-ld=lld in its own "
            "cargo config will fail this build with \"cannot find 'ld'\""
        )

    # Checked before the workload rather than after it: without profdata the
    # optimise step fails, and the 300s of profiling is spent for nothing.
    if not _ensure_llvm_profdata_available():
        error(
            "llvm-profdata is unavailable and cargo-pgo cannot merge the "
            "profile without it -- refusing to spend the workload on a PGO "
            "build that cannot finish"
        )
        return 1

    # Every later stage links a binary at least as large as the instrumented
    # one, so a linker that rescues this build has to carry forward.
    build_env = extra_env

    # 1. Instrumented build
    info(f"PGO: building instrumented binary for {target}")
    instrument_args = ["build", "--", "--target", target, *feature_args]
    rc = _run_cargo_pgo(instrument_args, cwd=cwd, extra_env=build_env)
    if rc != 0 and target.startswith("aarch64") and shutil.which("mold"):
        # Profile counters push a large binary's text past the +/-128 MB
        # R_AARCH64_CALL26 branch, which bfd cannot bridge; mold inserts thunks.
        warn(
            "PGO instrumented build failed on aarch64 -- retrying once with mold, "
            "which bridges branches past the 128 MB limit"
        )
        build_env = {
            **(extra_env or {}),
            _target_rustflags_key(target): "-C link-arg=-fuse-ld=mold",
        }
        rc = _run_cargo_pgo(instrument_args, cwd=cwd, extra_env=build_env)
    if rc != 0:
        error(f"PGO instrumented build failed for {target}")
        return rc

    # 2. Run workload against the instrumented binary
    instrumented_bin = _instrumented_binary_path(
        cwd, target, binary_name, variant="pgo"
    )
    if not instrumented_bin.exists():
        error(f"Instrumented binary not found at {instrumented_bin}")
        return 1

    if profile.pgo_workload_setup_cmd:
        rc = _run_workload_setup(profile.pgo_workload_setup_cmd, cwd)
        if rc != 0:
            error("PGO workload setup failed — aborting before profiling")
            return rc

    rc = _run_workload(
        profile.pgo_workload_cmd or "",
        profile.pgo_duration_secs,
        instrumented_bin,
        cwd=cwd,
    )
    if rc != 0:
        error("PGO workload failed — aborting (bad profile data is worse than no PGO)")
        return rc

    # 3. Optimised build using profile data
    info(f"PGO: building optimised binary for {target}")
    rc = _run_cargo_pgo(
        ["optimize", "--", "--target", target, *feature_args],
        cwd=cwd,
        extra_env=build_env,
    )
    if rc != 0:
        error(f"PGO optimised build failed for {target}")
        return rc
    if outcome:
        outcome.pgo_applied = True

    # 4. BOLT (optional, Linux-only)
    if profile.bolt_enabled:
        rc = _run_bolt(
            target, feature_args, binary_name, profile, cwd, build_env, outcome
        )
        if rc != 0:
            warn("BOLT step failed — continuing with PGO-only optimised binary")
            # BOLT failure is non-fatal; PGO binary is already built

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_plain_release_build(
    target: str,
    feature_args: list[str],
    cwd: Path,
    extra_env: dict[str, str] | None,
) -> int:
    """Fallback plain `cargo build --release` when PGO tooling unavailable.

    Tier 1 optimisations (allocator features, LTO env overrides) are still
    applied via `feature_args` and `extra_env` -- only PGO/BOLT are skipped.
    """
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    cmd = ["cargo", "build", "--release", "--target", target, *feature_args]
    info(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, env=env, check=False)
    return result.returncode


def cargo_pgo_version_from(output: str) -> str | None:
    """Pull the version out of `cargo pgo --version` output, or None."""
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", output)
    return match.group(1) if match else None


def _installed_cargo_pgo_version() -> str | None:
    """Version of the cargo-pgo on PATH, or None when absent or unreadable."""
    if not shutil.which("cargo-pgo"):
        return None
    result = run_cmd(["cargo", "pgo", "--version"], capture=True, check=False)
    if result.returncode != 0:
        return None
    return cargo_pgo_version_from(result.stdout)


def _ensure_cargo_pgo_installed() -> bool:
    """Make the pinned cargo-pgo available, installing it when absent or different.

    The version comes from `tools.cargo-pgo` in versions.yaml, so the tool that
    instruments and rewrites the release binary is the one we reviewed, not
    whatever crates.io serves on the day. A persistent runner home can carry an
    older build, so a version mismatch reinstalls too.

    Returns True if cargo-pgo is available after this call. Ensures
    `~/.cargo/bin` is on PATH so subsequent `cargo pgo` subprocess
    calls find the freshly-installed binary. CI runners sometimes ship
    with `~/.cargo/bin` absent from PATH even though it's the cargo
    install default.
    """
    pinned = tool_version("cargo-pgo")
    installed = _installed_cargo_pgo_version()
    if installed == pinned:
        return True

    # Ensure ~/.cargo/bin is on PATH before install -- cargo writes there
    cargo_bin = Path.home() / ".cargo" / "bin"
    current_path = os.environ.get("PATH", "")
    if str(cargo_bin) not in current_path.split(os.pathsep):
        os.environ["PATH"] = f"{cargo_bin}{os.pathsep}{current_path}"

    install = ["cargo", "install", "cargo-pgo", "--version", pinned, "--locked"]
    found = f"found {installed}" if installed else "not found"
    info(f"cargo-pgo {found}, pinned {pinned} — installing with '{' '.join(install)}'")
    result = subprocess.run(install, check=False)
    if result.returncode != 0:
        warn("cargo-pgo install failed")
        return False

    # Re-check with PATH that now includes ~/.cargo/bin
    if shutil.which("cargo-pgo"):
        return True

    # Last-ditch: check the absolute path
    direct_path = cargo_bin / "cargo-pgo"
    if direct_path.exists() and os.access(direct_path, os.X_OK):
        info(f"cargo-pgo found at {direct_path} (PATH did not include ~/.cargo/bin)")
        return True

    warn(
        "cargo-pgo installed successfully but not discoverable on PATH; "
        "check runner environment"
    )
    return False


_BOLT_TOOLCHAIN_BINARIES = ("llvm-bolt", "merge-fdata", "ld.lld")


def _ensure_llvm_bolt_available() -> bool:
    """Check BOLT toolchain is discoverable; shim versioned binaries onto PATH.

    Ubuntu's `bolt-NN` apt package installs version-suffixed binaries
    (e.g. `/usr/bin/llvm-bolt-23`, `/usr/bin/merge-fdata-23`) but NO
    unversioned symlinks -- and cargo-pgo's BOLT flow invokes the
    unversioned names (`llvm-bolt` AND `merge-fdata`, the latter to
    merge BOLT profile fragments before applying them).

    For each toolchain binary, try the plain name first; if missing,
    find the version-suffixed variant and create a symlink in
    `~/.local/bin` so subsequent subprocess invocations resolve the
    unversioned name. All shimmed binaries must share the same LLVM
    major version -- we pick the version that provides `llvm-bolt`
    (preferring HYPERCI_LLVM_VERSION) and shim `merge-fdata` from the
    same version for consistency.

    Returns True only if every required binary is discoverable (either
    directly or via shim). cargo-pgo's BOLT step fails silently on
    partial toolchain -- all-or-nothing is the safer contract.
    No auto-install -- the apt package is added by native_deps.py.
    """
    return _shim_llvm_tools(_BOLT_TOOLCHAIN_BINARIES)


def _rustc_sysroot_bin() -> Path | None:
    """Directory the `llvm-tools` component installs its binaries into.

    A missing rustc RAISES rather than returning non-zero, so the absent case
    is caught here instead of reaching the caller as a traceback.
    """
    try:
        sysroot = run_cmd(["rustc", "--print", "sysroot"], check=False, capture=True)
        version = run_cmd(["rustc", "-vV"], check=False, capture=True)
    except OSError:
        return None
    if sysroot.returncode != 0 or not sysroot.stdout.strip():
        return None
    if version.returncode != 0:
        return None
    host = next(
        (
            line.split("host:", 1)[1].strip()
            for line in version.stdout.splitlines()
            if line.startswith("host:")
        ),
        "",
    )
    if not host:
        return None
    return Path(sysroot.stdout.strip()) / "lib" / "rustlib" / host / "bin"


def _ensure_llvm_profdata_available() -> bool:
    """Make `llvm-profdata` resolvable so cargo-pgo can merge the profile.

    The `llvm-tools-preview` rustup component installs it under the rustc
    sysroot and NOT on PATH, and a self-hosted runner skips the setup action
    that would have added the component at all.

    A distro `llvm-profdata` on PATH is taken first and is normally fine -- an
    older one reads a profile written by a newer LLVM. The sysroot copy is the
    FALLBACK because rustup guarantees it exists and it matches the compiler,
    which is what makes this work on a GitHub-hosted runner that ships no
    unversioned llvm-profdata at all.
    """
    found = shutil.which("llvm-profdata")
    if found:
        info(f"  llvm-profdata: {found} (on PATH)")
        return True

    bin_dir = _rustc_sysroot_bin()
    if bin_dir is None:
        warn("  could not resolve the rustc sysroot to look for llvm-profdata")
        return False

    if not (bin_dir / "llvm-profdata").exists():
        info("  llvm-profdata missing - adding the llvm-tools-preview component")
        added = run_cmd(
            ["rustup", "component", "add", "llvm-tools-preview"], check=False
        )
        if added.returncode != 0:
            warn("  rustup could not add llvm-tools-preview")
            return False

    if not (bin_dir / "llvm-profdata").exists():
        return False

    current = os.environ.get("PATH", "")
    if str(bin_dir) not in current.split(os.pathsep):
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{current}"
    info(f"  llvm-profdata: {bin_dir / 'llvm-profdata'}")
    return True


def _ensure_ld_lld_available() -> bool:
    """Put an unversioned `ld.lld` on PATH when only `ld.lld-NN` is installed.

    gcc's `-fuse-ld=lld` looks for a binary named exactly `ld.lld`, and the
    `lld-NN` apt package ships only the suffixed one. Run before the PGO steps,
    so a project that selects lld in its own cargo config links in every
    stage, not only the BOLT ones that shimmed it before.
    """
    return _shim_llvm_tools(("ld.lld",))


def _shim_llvm_tools(names: tuple[str, ...]) -> bool:
    """Make every tool in ``names`` resolvable unversioned, from ONE LLVM version.

    Returns True when all of them resolve, directly or through a symlink in
    `~/.local/bin` to the version-suffixed binary.
    """
    # Fast path: all unversioned binaries already on PATH.
    if all(shutil.which(name) for name in names):
        return True

    # Prefer the version pinned in HYPERCI_LLVM_VERSION (matches the
    # version the apt installer targeted), then fall back to a descending
    # range. Range covers LLVM 18..30 which spans Ubuntu jammy through
    # expected future releases.
    preferred = os.environ.get("HYPERCI_LLVM_VERSION")
    preferred_int: int | None = None
    try:
        preferred_int = int(preferred) if preferred else None
    except ValueError:
        preferred_int = None

    versions: list[int] = []
    if preferred_int is not None:
        versions.append(preferred_int)
    versions.extend(v for v in range(30, 17, -1) if v != preferred_int)

    shim_dir = Path.home() / ".local" / "bin"
    for version in versions:
        # Require that THIS version provides every needed binary so the
        # shimmed toolchain is internally consistent.
        resolved: dict[str, str] = {}
        for name in names:
            versioned = shutil.which(f"{name}-{version}")
            if versioned:
                resolved[name] = versioned

        if len(resolved) != len(names):
            continue

        shim_dir.mkdir(parents=True, exist_ok=True)
        for name, versioned in resolved.items():
            shim = shim_dir / name
            if shim.exists() or shim.is_symlink():
                shim.unlink()
            shim.symlink_to(versioned)
            info(f"{name} shim: {shim} -> {versioned}")

        # Ensure ~/.local/bin is on PATH for subprocess children
        current_path = os.environ.get("PATH", "")
        if str(shim_dir) not in current_path.split(os.pathsep):
            os.environ["PATH"] = f"{shim_dir}{os.pathsep}{current_path}"

        return True

    return False


def _run_cargo_pgo(
    args: list[str],
    cwd: Path,
    extra_env: dict[str, str] | None,
) -> int:
    """Run a `cargo pgo <args>` command with merged env."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    cmd = ["cargo", "pgo", *args]
    info(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, env=env, check=False)
    return result.returncode


# Bounds the setup step on its own clock, separate from the workload's grace:
# building a load driver can take far longer than the profiling run.
_WORKLOAD_SETUP_TIMEOUT_SECS = 3600


def _run_workload_setup(setup_cmd: str, cwd: Path) -> int:
    """Run the project's workload setup before the workload clock starts.

    `build.rust.optimize.pgo.workload_setup_cmd` is for work that must finish
    before profiling and would not fit in the workload's grace -- building a
    load driver, pulling images. It runs once per target with its own timeout.
    """
    info(f"  $ {setup_cmd}  (workload setup, timeout={_WORKLOAD_SETUP_TIMEOUT_SECS}s)")
    try:
        result = subprocess.run(
            setup_cmd,
            shell=True,  # noqa: S602  # nosemgrep: subprocess-shell-true -- project-owned config, run in controlled CI env
            cwd=cwd,
            env=dict(os.environ),
            check=False,
            timeout=_WORKLOAD_SETUP_TIMEOUT_SECS,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        error(f"PGO workload setup exceeded {_WORKLOAD_SETUP_TIMEOUT_SECS}s")
        return 1


def _run_workload(
    workload_cmd: str,
    duration_secs: int,
    instrumented_binary: Path,
    cwd: Path,
) -> int:
    """Run the project's PGO workload command against the instrumented binary.

    Contract with consumer workload scripts:
      * Binary path is the **first positional argument** (`$1`). Matches
        the Unix-idiomatic pattern used by every template in
        `hyperi-ci/templates/pgo-workload/`.
      * `HYPERCI_PGO_INSTRUMENTED_BINARY` env var is ALSO exported as a
        convenience for scripts that prefer to read it from env.
      * `PGO_WORKLOAD_DURATION_SECS` carries `duration_secs`, the variable
        every workload template reads for how long to drive the binary.

    Enforces a hard timeout at `duration_secs + 600` (10-minute absolute
    grace for setup overhead: spinning up testcontainers, cargo-building
    feature-gated drivers, waiting for readiness, cleaning up). This is
    generous on purpose -- the workload script is trusted and should
    self-terminate at `duration_secs`; the wrapper timeout is a safety
    net that triggers only when the script hangs.
    """
    if not workload_cmd:
        error("PGO enabled but workload_cmd is empty")
        return 1

    env = dict(os.environ)
    env["HYPERCI_PGO_INSTRUMENTED_BINARY"] = str(instrumented_binary)
    env["PGO_WORKLOAD_DURATION_SECS"] = str(duration_secs)

    # Append the binary path as the first positional argument. Shell
    # quoting handled by shlex.quote so paths with spaces don't break.
    import shlex as _shlex

    full_cmd = f"{workload_cmd} {_shlex.quote(str(instrumented_binary))}"

    timeout_secs = duration_secs + 600
    info(f"  $ {full_cmd}  (timeout={timeout_secs}s = duration+600s safety grace)")
    try:
        result = subprocess.run(
            full_cmd,
            shell=True,  # noqa: S602  # nosemgrep: subprocess-shell-true -- workload_cmd is project-owned config, run in controlled CI env
            cwd=cwd,
            env=env,
            check=False,
            timeout=timeout_secs,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        error(
            f"PGO workload exceeded {timeout_secs}s timeout — "
            "workload should self-terminate at duration_secs"
        )
        return 1


def _release_dir(cwd: Path, target: str) -> Path:
    """Directory cargo writes `--release --target <target>` output to.

    Honours CARGO_TARGET_DIR the same way packaging does, so every PGO and
    BOLT step looks where the packager later copies from.
    """
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR") or "target")
    if not target_dir.is_absolute():
        target_dir = cwd / target_dir
    return target_dir / target / "release"


def _instrumented_binary_path(
    cwd: Path,
    target: str,
    binary_name: str,
    variant: str,
) -> Path:
    """Locate the instrumented binary produced by cargo pgo.

    Both phases build under the release directory. The PGO instrument
    build keeps the plain binary name; the BOLT instrument build suffixes
    it `-bolt-instrumented` (cargo-pgo convention). Profile data goes into
    target/pgo-profiles/ (handled by cargo-pgo, not this code).
    """
    name = f"{binary_name}-bolt-instrumented" if variant == "bolt" else binary_name
    return _release_dir(cwd, target) / name


def _install_bolt_output(cwd: Path, target: str, binary_name: str) -> bool:
    """Put BOLT's rewritten binary where packaging copies from.

    `cargo pgo bolt optimize` leaves the BOLT result beside the cargo output
    as `<bin>-bolt-optimized`; the unsuffixed file is the PGO-only build of
    the same pass, and packaging ships the unsuffixed name.

    Returns:
        True only when the BOLT file exists, carries llvm-bolt's note, and
        now sits at the unsuffixed path.

    """
    release = _release_dir(cwd, target)
    optimized = release / f"{binary_name}-bolt-optimized"
    if not optimized.is_file():
        warn(
            f"BOLT optimise reported success but {optimized} does not exist -- "
            "shipping the PGO-only binary"
        )
        return False
    if BOLT_NOTE_SECTION not in elf_section_names(optimized):
        warn(
            f"{optimized} carries no {BOLT_NOTE_SECTION}, so it is not BOLT "
            "output -- shipping the PGO-only binary"
        )
        return False
    shutil.copy2(optimized, release / binary_name)
    info(f"BOLT: {optimized.name} installed as {binary_name} for packaging")
    return True


# RUSTFLAGS used on the no-split BOLT RETRY (see _run_bolt). BOLT in relocation
# mode CANNOT process a binary whose functions the compiler already hot/cold-SPLIT
# - it emits `<fn>.cold` fragments (e.g. a closure's drop glue outlined into
# .text.unlikely/.text.split) and llvm-bolt aborts the step with:
#   BOLT-WARNING: split function detected on input ... limited in relocation mode
#   BOLT-ERROR:   parent function not found for <fn>.cold
# BOLT wants to do the splitting ITSELF, so the fix is to stop the COMPILER
# pre-splitting for the BOLT build (the LLVM equivalent of clang's
# -fno-reorder-blocks-and-partition), via -Cllvm-args. This is applied ONLY on a
# RETRY after a first BOLT attempt fails (see _run_bolt), so apps that already
# BOLT-optimise cleanly never see it - a working BOLT layer is the default
# WITHOUT risking the apps that don't need the flag.
#
# The exact cl::opt is toolchain-version-dependent; this targets the PGO-driven
# cold-splitter. Override (or disable the retry, with "") via
# HYPERCI_BOLT_EXTRA_RUSTFLAGS; alternative candidate if this proves ineffective:
# -Cllvm-args=-split-machine-functions=false. BOLT failure is non-fatal, so an
# ineffective value simply leaves that one app PGO-only - it cannot break a build.
_DEFAULT_BOLT_NO_SPLIT_RUSTFLAGS = "-Cllvm-args=-hot-cold-split=false"


def _bolt_no_split_rustflags() -> str:
    """RUSTFLAGS for the no-split BOLT retry.

    Defaults to disabling the compiler cold-splitter. An empty
    HYPERCI_BOLT_EXTRA_RUSTFLAGS disables the retry entirely.
    """
    val = os.environ.get("HYPERCI_BOLT_EXTRA_RUSTFLAGS")
    return _DEFAULT_BOLT_NO_SPLIT_RUSTFLAGS if val is None else val.strip()


def _target_rustflags_key(target: str) -> str:
    """Cargo's env name for `target.<triple>.rustflags`.

    UPPERCASE with hyphens and dots as underscores, so
    x86_64-unknown-linux-gnu becomes CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS.
    """
    return (
        f"CARGO_TARGET_{target.upper().replace('-', '_').replace('.', '_')}_RUSTFLAGS"
    )


def _bolt_build_env(target: str, *, no_split: bool = False) -> dict[str, str]:
    """Env overrides for cargo-pgo BOLT build and optimize steps.

    BOLT imposes two linker-level requirements that collide with common
    release-profile settings:

    1. **Linker must be lld.** BOLT's instrumented builds pass
       `-Wl,-q` (`--emit-relocs`) which mold segfaults on and GNU BFD
       rejects. lld is the canonical BOLT-compatible linker.

    2. **strip must be disabled.** Rust's `[profile.release] strip = true`
       appends `-Wl,--strip-all` to the link, which lld refuses to
       combine with `--emit-relocs`. We override via
       `CARGO_PROFILE_RELEASE_STRIP=none` for the BOLT steps only --
       the project's regular release build keeps whatever strip
       setting it declared. Final binary is stripped by hyperi-ci's
       post-build packaging separately, so dropping cargo-level strip
       here doesn't bloat the shipped artefact.

    These overrides apply to both the instrumented build (used only
    for profile collection) and the final BOLT-optimized build.

    Cargo joins the env value onto the project's `target.<triple>.rustflags`,
    so flags declared there (`-C target-cpu=x86-64-v3`) survive and the later
    `-fuse-ld=lld` wins over a project `-fuse-ld=mold`. A project whose flags
    live only in `build.rustflags` loses them for these steps: cargo reads
    `build.rustflags` only when no target rustflags exist.
    """
    target_rustflags_key = _target_rustflags_key(target)
    # Base BOLT rustflags (lld for --emit-relocs). On the no-split retry, append
    # the splitter-disabling flags (see _bolt_no_split_rustflags / _run_bolt).
    bolt_rustflags = "-C link-arg=-fuse-ld=lld"
    if no_split:
        extra = _bolt_no_split_rustflags()
        if extra:
            bolt_rustflags = f"{bolt_rustflags} {extra}"
    return {
        target_rustflags_key: bolt_rustflags,
        "CARGO_PROFILE_RELEASE_STRIP": "none",
    }


# BOLT refuses an aarch64 binary carrying the linker's Cortex-A53 erratum 843419
# workaround veneers, because relaying the binary invalidates the page offsets
# those veneers were computed from. Dropping them leaves the shipped binary
# unsafe on Cortex-A53, which is accepted because these binaries run on Graviton
# and Ampere-class server cores, never the 2012 in-order A53 little core used in
# phones and embedded parts. A deployment target that does include Cortex-A53
# has to drop this flag and take PGO-only aarch64 builds.
_DROP_A53_VENEERS = "--drop-cortex-a53-843419-veneers"

# cargo-pgo passes --bolt-args to llvm-bolt in place of the flags it would
# otherwise pass, not alongside them, so its own defaults are restated here and
# sent back with the extra flag. They are copied from the pinned cargo-pgo
# (`tools.cargo-pgo` in versions.yaml), src/bolt/instrument.rs and
# src/bolt/optimize.rs, and move with that pin.
#
# tests/unit/test_rust_pgo.py fails when `tools.cargo-pgo` moves away from the
# version the two tuples below were read from.
_CARGO_PGO_FLAGS_VERIFIED_AGAINST = "0.3.0"

_CARGO_PGO_INSTRUMENT_BOLT_ARGS = ("-update-debug-sections",)
_CARGO_PGO_OPTIMIZE_BOLT_ARGS = (
    "-reorder-blocks=ext-tsp",
    "-reorder-functions=hfsort",
    "-split-functions=2",
    "-split-all-cold",
    "-jump-tables=move",
    "-use-gnu-stack",
    "-split-eh",
    "-lite=1",
    "-icf=1",
    "-relocs",
    "-update-debug-sections",
    "-dyno-stats",
)


def _bolt_tool_args(target: str, stage: Literal["instrument", "optimize"]) -> list[str]:
    """`--bolt-args` for one cargo-pgo BOLT stage, empty off aarch64.

    The two stages take different default flag sets, and `--bolt-args`
    replaces rather than extends them, so the set for `stage` is sent back
    with the veneer flag appended. Every other architecture gets an empty
    list and keeps cargo-pgo's defaults with nothing passed through.
    """
    if not target.startswith("aarch64"):
        return []
    defaults = (
        _CARGO_PGO_INSTRUMENT_BOLT_ARGS
        if stage == "instrument"
        else _CARGO_PGO_OPTIMIZE_BOLT_ARGS
    )
    return ["--bolt-args", " ".join([*defaults, _DROP_A53_VENEERS])]


def _attempt_bolt(
    target: str,
    feature_args: list[str],
    binary_name: str,
    profile: OptimizationProfile,
    cwd: Path,
    extra_env: dict[str, str] | None,
    *,
    no_split: bool,
    outcome: OptimizationOutcome | None = None,
) -> int:
    """Run one BOLT pass: instrument → workload → optimise.

    `bolt build` emits `<binary>-bolt-instrumented`; the workload must run
    against THAT binary so BOLT collects its own branch profile. Skipping
    the workload (the old behaviour) left `bolt optimize` with nothing to
    optimise -- see #29.

    Forces lld as the linker and disables strip for both build phases --
    see `_bolt_build_env()`. When `no_split` is True, also disables the
    compiler cold-splitter so BOLT can process the binary (see _run_bolt).

    Returns 0 on success OR a non-fatal skip (missing instrumented binary or
    a failed workload -- PGO-only result stands). Returns non-zero only on a
    BOLT BUILD failure (instrument or optimise), which _run_bolt retries.
    """
    # Merge project env_overrides (LTO etc.) with the BOLT-step build
    # env (fuse-ld=lld + strip=none [+ no-split]). BOLT env takes precedence
    # over project config for the target-specific rustflags -- intentional.
    bolt_env = {**(extra_env or {}), **_bolt_build_env(target, no_split=no_split)}
    label = " (no-split)" if no_split else ""

    if target.startswith("aarch64"):
        info(
            f"BOLT: dropping Cortex-A53 erratum 843419 veneers for {target} -- "
            "the shipped binary is not safe on Cortex-A53"
        )

    # 1. BOLT instrument build
    info(
        f"BOLT: building instrumented binary for {target} (linker forced to lld){label}"
    )
    rc = _run_cargo_pgo(
        [
            "bolt",
            "build",
            *_bolt_tool_args(target, "instrument"),
            "--",
            "--target",
            target,
            *feature_args,
        ],
        cwd=cwd,
        extra_env=bolt_env,
    )
    if rc != 0:
        return rc

    # 2. Run the workload against the bolt-instrumented binary to collect
    #    BOLT's own profile. Without this, `bolt optimize` has no data.
    bolt_bin = _instrumented_binary_path(cwd, target, binary_name, variant="bolt")
    if not bolt_bin.exists():
        warn(
            f"BOLT-instrumented binary not found at {bolt_bin} — "
            "skipping BOLT (PGO-only result stands)"
        )
        return 0  # Non-fatal
    rc = _run_workload(
        profile.pgo_workload_cmd or "",
        profile.pgo_duration_secs,
        bolt_bin,
        cwd=cwd,
    )
    if rc != 0:
        warn("BOLT workload failed — skipping BOLT optimise (PGO-only result stands)")
        return 0  # Non-fatal: PGO binary already built

    # 3. BOLT optimise, folding in both the PGO and BOLT profiles
    info(
        f"BOLT: optimising binary for {target} (using PGO + BOLT profiles, linker=lld){label}"
    )
    rc = _run_cargo_pgo(
        [
            "bolt",
            "optimize",
            "--with-pgo",
            *_bolt_tool_args(target, "optimize"),
            "--",
            "--target",
            target,
            *feature_args,
        ],
        cwd=cwd,
        extra_env=bolt_env,
    )
    # Only this branch can produce a BOLT-optimised binary -- the returns above
    # are non-fatal skips that also report 0 -- and it counts only once the
    # BOLT file is where packaging will pick it up.
    if rc == 0 and _install_bolt_output(cwd, target, binary_name) and outcome:
        outcome.bolt_applied = True
    return rc


def _run_bolt(
    target: str,
    feature_args: list[str],
    binary_name: str,
    profile: OptimizationProfile,
    cwd: Path,
    extra_env: dict[str, str] | None,
    outcome: OptimizationOutcome | None = None,
) -> int:
    """Run BOLT, retrying once with compiler function-splitting disabled.

    The first attempt mirrors the project's normal build. If the BOLT BUILD
    fails -- most commonly because the compiler pre-split a function into a
    `.cold` fragment that BOLT can't process in relocation mode -- retry once
    with the splitter disabled so BOLT splits the binary itself. Apps whose
    first attempt succeeds never retry, so they are completely unaffected:
    this makes a working BOLT layer the default WITHOUT risking the apps that
    already optimise cleanly, and degrades to PGO-only (non-fatal) if neither
    attempt succeeds.

    Requires the llvm-bolt + merge-fdata + ld.lld toolchain installed
    (covered by the `bolt-NN` + `lld-NN` apt packages from apt.llvm.org).
    Silent skip if any toolchain binary is missing.
    """
    if not _ensure_llvm_bolt_available():
        warn(
            "BOLT toolchain not complete (llvm-bolt / merge-fdata / ld.lld) — skipping BOLT step"
        )
        return 0  # Non-fatal

    rc = _attempt_bolt(
        target,
        feature_args,
        binary_name,
        profile,
        cwd,
        extra_env,
        no_split=False,
        outcome=outcome,
    )
    if rc == 0:
        return 0

    no_split_flags = _bolt_no_split_rustflags()
    if not no_split_flags:
        # Retry explicitly disabled via HYPERCI_BOLT_EXTRA_RUSTFLAGS="".
        return rc
    warn(
        "BOLT build failed — retrying once with compiler function-splitting "
        f"disabled ({no_split_flags})"
    )
    return _attempt_bolt(
        target,
        feature_args,
        binary_name,
        profile,
        cwd,
        extra_env,
        no_split=True,
        outcome=outcome,
    )
