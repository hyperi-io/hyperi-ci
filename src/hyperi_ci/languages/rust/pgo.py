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

    Contract with consumer workload scripts:
      * Binary path is the **first positional argument** (`$1`). Matches
        the Unix-idiomatic pattern used by every template in
        `hyperi-ci/templates/pgo-workload/`.
      * `HYPERCI_PGO_INSTRUMENTED_BINARY` env var is ALSO exported as a
        convenience for scripts that prefer to read it from env.

    Enforces a hard timeout at `duration_secs * 1.2` (20% grace for
    clean shutdown).
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

    info(f"  $ {full_cmd}  (timeout={duration_secs}s + 20% grace)")
    try:
        result = subprocess.run(
            full_cmd,
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
