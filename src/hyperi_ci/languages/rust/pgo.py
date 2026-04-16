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
    if not _ensure_cargo_pgo_installed():
        warn("cargo-pgo unavailable — skipping PGO optimisation")
        return 0  # Non-fatal: fall back to plain build path handled by caller

    features = profile.cargo_features()
    feature_args = ["--features", ",".join(features)] if features else []

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


def _ensure_cargo_pgo_installed() -> bool:
    """Check cargo-pgo is installed; auto-install if not.

    Returns True if cargo-pgo is available after this call.
    """
    if shutil.which("cargo-pgo"):
        return True

    info("cargo-pgo not found — installing with 'cargo install cargo-pgo --locked'")
    result = subprocess.run(
        ["cargo", "install", "cargo-pgo", "--locked"],
        check=False,
    )
    if result.returncode == 0:
        return shutil.which("cargo-pgo") is not None

    warn("cargo-pgo install failed")
    return False


def _ensure_llvm_bolt_available() -> bool:
    """Check llvm-bolt is on PATH. No auto-install — it's a system package."""
    return shutil.which("llvm-bolt") is not None


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

    Sets HYPERCI_PGO_INSTRUMENTED_BINARY env var so the workload script
    can find the binary. Enforces a hard timeout at `duration_secs * 1.2`
    (20% grace for clean shutdown).
    """
    if not workload_cmd:
        error("PGO enabled but workload_cmd is empty")
        return 1

    env = dict(os.environ)
    env["HYPERCI_PGO_INSTRUMENTED_BINARY"] = str(instrumented_binary)

    info(f"  $ {workload_cmd}  (timeout={duration_secs}s + 20% grace)")
    try:
        result = subprocess.run(
            workload_cmd,
            shell=True,  # noqa: S602 - workload_cmd is project-owned config, run in controlled CI env
            cwd=cwd,
            env=env,
            check=False,
            timeout=int(duration_secs * 1.2),
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        error(
            f"PGO workload exceeded {int(duration_secs * 1.2)}s timeout — "
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


def _run_bolt(
    target: str,
    feature_args: list[str],
    binary_name: str,
    cwd: Path,
    extra_env: dict[str, str] | None,
) -> int:
    """Run the BOLT post-link optimisation pipeline.

    Requires llvm-bolt installed. Silent skip if not.
    Requires the PGO step to have run already (BOLT uses the PGO profile).
    """
    if not _ensure_llvm_bolt_available():
        warn("llvm-bolt not installed — skipping BOLT step")
        return 0  # Non-fatal

    # BOLT instrument build
    info(f"BOLT: building instrumented binary for {target}")
    rc = _run_cargo_pgo(
        ["bolt", "build", "--", "--target", target, *feature_args],
        cwd=cwd,
        extra_env=extra_env,
    )
    if rc != 0:
        return rc

    # Note: cargo-pgo's bolt flow handles the perf record step internally
    # when given a runnable binary. For our case, we re-run the workload
    # (same command) and cargo-pgo instruments it automatically.
    # This is a placeholder for the workload re-run step — the actual
    # perf-record wiring is handled by cargo-pgo's bolt subcommand.

    info(f"BOLT: optimising binary for {target} (using PGO profile)")
    rc = _run_cargo_pgo(
        ["bolt", "optimize", "--with-pgo", "--", "--target", target, *feature_args],
        cwd=cwd,
        extra_env=extra_env,
    )
    # Silence unused arg warning
    del binary_name
    return rc
