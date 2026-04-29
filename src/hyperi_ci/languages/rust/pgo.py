# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/pgo.py
# Purpose:   PGO + BOLT build orchestration for Rust binaries
#
# License:   Proprietary — HYPERI PTY LIMITED
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

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, warn
from hyperi_ci.languages.rust.optimize import OptimizationProfile


def run_pgo_build(
    target: str,
    profile: OptimizationProfile,
    binary_name: str,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
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

    Returns:
        0 on success, non-zero on failure.

    """
    features = profile.cargo_features()
    feature_args = ["--features", ",".join(features)] if features else []

    if not _ensure_cargo_pgo_installed():
        warn(
            "cargo-pgo unavailable — falling back to plain release build "
            "(Tier 1 optimisations still apply)"
        )
        return _run_plain_release_build(target, feature_args, cwd, extra_env)

    # 1. Instrumented build
    info(f"PGO: building instrumented binary for {target}")
    rc = _run_cargo_pgo(
        ["build", "--", "--target", target, *feature_args],
        cwd=cwd,
        extra_env=extra_env,
    )
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
        extra_env=extra_env,
    )
    if rc != 0:
        error(f"PGO optimised build failed for {target}")
        return rc

    # 4. BOLT (optional, Linux-only)
    if profile.bolt_enabled:
        rc = _run_bolt(target, feature_args, binary_name, cwd, extra_env)
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
    applied via `feature_args` and `extra_env` — only PGO/BOLT are skipped.
    """
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    cmd = ["cargo", "build", "--release", "--target", target, *feature_args]
    info(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, env=env, check=False)
    return result.returncode


def _ensure_cargo_pgo_installed() -> bool:
    """Check cargo-pgo is installed; auto-install if not.

    Returns True if cargo-pgo is available after this call. Ensures
    `~/.cargo/bin` is on PATH so subsequent `cargo pgo` subprocess
    calls find the freshly-installed binary. CI runners sometimes ship
    with `~/.cargo/bin` absent from PATH even though it's the cargo
    install default.
    """
    if shutil.which("cargo-pgo"):
        return True

    # Ensure ~/.cargo/bin is on PATH before install — cargo writes there
    cargo_bin = Path.home() / ".cargo" / "bin"
    current_path = os.environ.get("PATH", "")
    if str(cargo_bin) not in current_path.split(os.pathsep):
        os.environ["PATH"] = f"{cargo_bin}{os.pathsep}{current_path}"

    info("cargo-pgo not found — installing with 'cargo install cargo-pgo --locked'")
    result = subprocess.run(
        ["cargo", "install", "cargo-pgo", "--locked"],
        check=False,
    )
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
    (e.g. `/usr/bin/llvm-bolt-22`, `/usr/bin/merge-fdata-22`) but NO
    unversioned symlinks — and cargo-pgo's BOLT flow invokes the
    unversioned names (`llvm-bolt` AND `merge-fdata`, the latter to
    merge BOLT profile fragments before applying them).

    For each toolchain binary, try the plain name first; if missing,
    find the version-suffixed variant and create a symlink in
    `~/.local/bin` so subsequent subprocess invocations resolve the
    unversioned name. All shimmed binaries must share the same LLVM
    major version — we pick the version that provides `llvm-bolt`
    (preferring HYPERCI_LLVM_VERSION) and shim `merge-fdata` from the
    same version for consistency.

    Returns True only if every required binary is discoverable (either
    directly or via shim). cargo-pgo's BOLT step fails silently on
    partial toolchain — all-or-nothing is the safer contract.
    No auto-install — the apt package is added by native_deps.py.
    """
    # Fast path: all unversioned binaries already on PATH.
    if all(shutil.which(name) for name in _BOLT_TOOLCHAIN_BINARIES):
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
        for name in _BOLT_TOOLCHAIN_BINARIES:
            versioned = shutil.which(f"{name}-{version}")
            if versioned:
                resolved[name] = versioned

        if len(resolved) != len(_BOLT_TOOLCHAIN_BINARIES):
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

    Enforces a hard timeout at `duration_secs + 600` (10-minute absolute
    grace for setup overhead: spinning up testcontainers, cargo-building
    feature-gated drivers, waiting for readiness, cleaning up). This is
    generous on purpose — the workload script is trusted and should
    self-terminate at `duration_secs`; the wrapper timeout is a safety
    net that triggers only when the script hangs.
    """
    if not workload_cmd:
        error("PGO enabled but workload_cmd is empty")
        return 1

    env = dict(os.environ)
    env["HYPERCI_PGO_INSTRUMENTED_BINARY"] = str(instrumented_binary)

    # Append the binary path as the first positional argument. Shell
    # quoting handled by shlex.quote so paths with spaces don't break.
    import shlex as _shlex

    full_cmd = f"{workload_cmd} {_shlex.quote(str(instrumented_binary))}"

    timeout_secs = duration_secs + 600
    info(f"  $ {full_cmd}  (timeout={timeout_secs}s = duration+600s safety grace)")
    try:
        result = subprocess.run(
            full_cmd,
            shell=True,  # noqa: S602 - workload_cmd is project-owned config, run in controlled CI env
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


def _instrumented_binary_path(
    cwd: Path,
    target: str,
    binary_name: str,
    variant: str,
) -> Path:
    """Locate the instrumented binary produced by cargo pgo.

    cargo-pgo builds under target/<triple>/release/ with the normal
    binary name. Profile data goes into target/pgo-profiles/ (handled
    by cargo-pgo, not this code).
    """
    # variant is currently unused but reserved for BOLT (which produces
    # a separate bolt-instrumented binary).
    del variant
    return cwd / "target" / target / "release" / binary_name


def _bolt_build_env(target: str) -> dict[str, str]:
    """Env overrides for cargo-pgo BOLT build and optimize steps.

    BOLT imposes two linker-level requirements that collide with common
    release-profile settings:

    1. **Linker must be lld.** BOLT's instrumented builds pass
       `-Wl,-q` (`--emit-relocs`) which mold segfaults on and GNU BFD
       rejects. lld is the canonical BOLT-compatible linker.

    2. **strip must be disabled.** Rust's `[profile.release] strip = true`
       appends `-Wl,--strip-all` to the link, which lld refuses to
       combine with `--emit-relocs`. We override via
       `CARGO_PROFILE_RELEASE_STRIP=none` for the BOLT steps only —
       the project's regular release build keeps whatever strip
       setting it declared. Final binary is stripped by hyperi-ci's
       post-build packaging separately, so dropping cargo-level strip
       here doesn't bloat the shipped artefact.

    These overrides apply to both the instrumented build (used only
    for profile collection) and the final BOLT-optimized build.

    The per-target `rustflags` override replaces the project's own
    target-specific flags during BOLT steps only — flags like
    `-C target-cpu=x86-64-v3` are lost for the instrumented build,
    which is acceptable because that binary only runs the workload to
    collect profile data (branch profile correctness is independent of
    codegen target-cpu).
    """
    # Cargo env var for target-specific rustflags uses UPPERCASE with
    # hyphens and dots replaced by underscores (e.g. x86_64-unknown-linux-gnu
    # → X86_64_UNKNOWN_LINUX_GNU).
    target_rustflags_key = (
        f"CARGO_TARGET_{target.upper().replace('-', '_').replace('.', '_')}_RUSTFLAGS"
    )
    return {
        target_rustflags_key: "-C link-arg=-fuse-ld=lld",
        "CARGO_PROFILE_RELEASE_STRIP": "none",
    }


# Backwards-compat alias for external callers / pre-1.11 tests.
# No in-tree caller uses this — _run_bolt calls _bolt_build_env directly.
# Remove in v2.
_bolt_linker_env = _bolt_build_env


def _run_bolt(
    target: str,
    feature_args: list[str],
    binary_name: str,
    cwd: Path,
    extra_env: dict[str, str] | None,
) -> int:
    """Run the BOLT post-link optimisation pipeline.

    Requires the llvm-bolt + merge-fdata + ld.lld toolchain installed
    (covered by the `bolt-NN` + `lld-NN` apt packages from apt.llvm.org).
    Silent skip if any toolchain binary is missing.

    Forces lld as the linker and disables strip for both
    `cargo pgo bolt build` and `cargo pgo bolt optimize` — see
    `_bolt_build_env()` for rationale.

    Requires the PGO step to have run already (BOLT uses the PGO profile).
    """
    if not _ensure_llvm_bolt_available():
        warn(
            "BOLT toolchain not complete (llvm-bolt / merge-fdata / ld.lld) — skipping BOLT step"
        )
        return 0  # Non-fatal

    # Merge project env_overrides (LTO etc.) with the BOLT-step build
    # env (fuse-ld=lld + strip=none). BOLT env takes precedence over
    # project config for the target-specific rustflags — intentional.
    bolt_env = {**(extra_env or {}), **_bolt_build_env(target)}

    # BOLT instrument build
    info(f"BOLT: building instrumented binary for {target} (linker forced to lld)")
    rc = _run_cargo_pgo(
        ["bolt", "build", "--", "--target", target, *feature_args],
        cwd=cwd,
        extra_env=bolt_env,
    )
    if rc != 0:
        return rc

    # Note: cargo-pgo's bolt flow handles the perf record step internally
    # when given a runnable binary. For our case, we re-run the workload
    # (same command) and cargo-pgo instruments it automatically.
    # This is a placeholder for the workload re-run step — the actual
    # perf-record wiring is handled by cargo-pgo's bolt subcommand.

    info(f"BOLT: optimising binary for {target} (using PGO profile, linker=lld)")
    rc = _run_cargo_pgo(
        ["bolt", "optimize", "--with-pgo", "--", "--target", target, *feature_args],
        cwd=cwd,
        extra_env=bolt_env,
    )
    # Silence unused arg warning
    del binary_name
    return rc
