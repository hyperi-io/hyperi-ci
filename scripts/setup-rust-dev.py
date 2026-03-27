#!/usr/bin/env python3
#  Project:      hyperi-ci
#  File:         scripts/setup-rust-dev.py
#  Purpose:      Set up Rust build optimisation on a developer workstation
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Implements the plan from docs/RUST-BUILD-OPTIMISATION.md:
#    1. Per-project target symlinks on /cache disk
#    2. Install and configure sccache
#    3. Install mold linker
#    4. Raise cargo parallelism (jobs 2 -> 8)
#    5. Create ~/.cargo/config.toml global config
#    6. Install cargo-sweep for cache hygiene
#    7. Patch per-project .cargo/config.toml files
#
#  Usage:
#    python3 scripts/setup-rust-dev.py              # Full setup
#    python3 scripts/setup-rust-dev.py --check       # Dry-run: show what would change
#    python3 scripts/setup-rust-dev.py --symlinks     # Only create target symlinks

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_DIR = Path("/cache")
CARGO_TARGETS_DIR = CACHE_DIR / "cargo-targets"
OLD_SHARED_TARGET = CACHE_DIR / "cargo-target"
PROJECTS_DIR = Path("/projects")
ETC_ENVIRONMENT = Path("/etc/environment")
CARGO_JOBS = 8

# Projects that have [target.x86_64-unknown-linux-gnu] rustflags and need
# mold linker flag injected alongside existing flags
PROJECTS_WITH_X86_RUSTFLAGS = [
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-elastic",
    "dfe-transform-splack",
    "dfe-transform-vector",
    "dfe-transform-vrl",
    "dfe-transform-wasm",
]

# Projects that have jobs = 2 (or 4) to remove
PROJECTS_WITH_JOBS = [
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-protocol-sdk",
    "dfe-receiver",
    "dfe-transform-elastic",
    "dfe-transform-splack",
    "dfe-transform-vector",
    "dfe-transform-vrl",
    "dfe-transform-wasm",
    "vrl",
]

# Do NOT touch these projects' configs
SKIP_PROJECTS = [
    "hyperi-rustlib",  # Complex cross-compile + clippy config
    "vrl",  # Upstream Vector fork, different conventions
]

MOLD_RUSTFLAG = '"-C", "link-arg=-fuse-ld=mold"'

GLOBAL_CARGO_CONFIG = """\
# Global Cargo configuration — managed by setup-rust-dev.py
#
# Per-project .cargo/config.toml files override these where they conflict.
# Cross-compilation settings remain in per-project configs.

[build]
jobs = {jobs}
rustc-wrapper = "sccache"

[target.x86_64-unknown-linux-gnu]
linker = "clang"
rustflags = ["-C", "link-arg=-fuse-ld=mold"]

# HyperI Private Cargo Registry (Artifactory)
[registries.hyperi]
index = "sparse+https://hypersec.jfrog.io/artifactory/api/cargo/hyperi-cargo-virtual/index/"
credential-provider = "cargo:token"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"  {msg}")


def log_section(msg: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}")


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kwargs)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def find_rust_projects() -> list[Path]:
    """Find all directories under /projects/ that contain a Cargo.toml."""
    return sorted(
        p.parent for p in PROJECTS_DIR.glob("*/Cargo.toml") if p.parent.is_dir()
    )


# ---------------------------------------------------------------------------
# Step 1: Remove CARGO_TARGET_DIR from /etc/environment
# ---------------------------------------------------------------------------


def remove_cargo_target_dir_env(dry_run: bool) -> None:
    log_section("Step 1: Remove CARGO_TARGET_DIR from /etc/environment")

    if not ETC_ENVIRONMENT.exists():
        log("  /etc/environment does not exist — skipping")
        return

    lines = ETC_ENVIRONMENT.read_text().splitlines()
    new_lines = [l for l in lines if not l.startswith("CARGO_TARGET_DIR=")]

    if len(new_lines) == len(lines):
        log("CARGO_TARGET_DIR not found in /etc/environment — already clean")
        return

    if dry_run:
        log("[DRY RUN] Would remove CARGO_TARGET_DIR from /etc/environment")
        return

    # Needs root — use sudo sed
    result = run(
        ["sudo", "sed", "-i", "/^CARGO_TARGET_DIR=/d", str(ETC_ENVIRONMENT)],
        check=False,
    )
    if result.returncode != 0:
        log(f"WARNING: Failed to edit /etc/environment: {result.stderr}")
    else:
        log("Removed CARGO_TARGET_DIR from /etc/environment")
    log("NOTE: Re-login or `source /etc/environment` to take effect")

    # Also unset for current process
    os.environ.pop("CARGO_TARGET_DIR", None)


# ---------------------------------------------------------------------------
# Step 2: Create per-project target symlinks on /cache
# ---------------------------------------------------------------------------


def create_target_symlinks(dry_run: bool) -> None:
    log_section("Step 2: Per-project target symlinks on /cache")

    projects = find_rust_projects()
    log(f"Found {len(projects)} Rust projects")

    if not CARGO_TARGETS_DIR.exists():
        if dry_run:
            log(f"[DRY RUN] Would create {CARGO_TARGETS_DIR}")
        else:
            import getpass

            user = getpass.getuser()
            run(["sudo", "mkdir", "-p", str(CARGO_TARGETS_DIR)])
            run(["sudo", "chown", f"{user}:{user}", str(CARGO_TARGETS_DIR)])
            log(f"Created {CARGO_TARGETS_DIR}")

    for proj in projects:
        name = proj.name
        target_on_cache = CARGO_TARGETS_DIR / name
        target_in_project = proj / "target"

        # Already a correct symlink?
        if target_in_project.is_symlink():
            current = target_in_project.resolve()
            if current == target_on_cache.resolve():
                log(f"  {name}: symlink already correct")
                continue
            else:
                if dry_run:
                    log(
                        f"  {name}: [DRY RUN] Would repoint symlink {current} -> {target_on_cache}"
                    )
                    continue
                target_in_project.unlink()

        if dry_run:
            log(f"  {name}: [DRY RUN] Would create symlink target -> {target_on_cache}")
            continue

        # Create target dir on cache disk
        target_on_cache.mkdir(parents=True, exist_ok=True)

        # Remove existing target dir if it's a real directory
        if target_in_project.is_dir() and not target_in_project.is_symlink():
            # Move contents to cache location first to preserve incremental cache
            log(f"  {name}: Moving existing target/ to {target_on_cache}")
            # Use rsync-style: copy contents then remove original
            for item in target_in_project.iterdir():
                dest = target_on_cache / item.name
                if not dest.exists():
                    item.rename(dest)
                else:
                    # Already exists on cache, skip
                    pass
            shutil.rmtree(target_in_project)

        target_in_project.symlink_to(target_on_cache)
        log(f"  {name}: Created symlink -> {target_on_cache}")


# ---------------------------------------------------------------------------
# Step 3: Install sccache
# ---------------------------------------------------------------------------


def install_sccache(dry_run: bool) -> None:
    log_section("Step 3: Install sccache")

    if command_exists("sccache"):
        result = run(["sccache", "--version"], check=False)
        version = result.stdout.strip() if result.returncode == 0 else "unknown"
        log(f"sccache already installed: {version}")
        return

    if dry_run:
        log("[DRY RUN] Would install sccache via cargo install")
        return

    log("Installing sccache (this may take a few minutes)...")
    result = run(["cargo", "install", "sccache", "--locked"], check=False)
    if result.returncode != 0:
        log(f"WARNING: sccache install failed: {result.stderr}")
    else:
        log("sccache installed")


# ---------------------------------------------------------------------------
# Step 4: Install mold linker
# ---------------------------------------------------------------------------


def install_mold(dry_run: bool) -> None:
    log_section("Step 4: Install mold linker")

    if command_exists("mold"):
        result = run(["mold", "--version"], check=False)
        version = result.stdout.strip() if result.returncode == 0 else "unknown"
        log(f"mold already installed: {version}")
        return

    if dry_run:
        log("[DRY RUN] Would install mold via apt")
        return

    log("Installing mold...")
    result = run(["sudo", "apt-get", "install", "-y", "mold"], check=False)
    if result.returncode != 0:
        log(f"WARNING: mold install failed: {result.stderr}")
    else:
        log("mold installed")


# ---------------------------------------------------------------------------
# Step 5: Install cargo-sweep
# ---------------------------------------------------------------------------


def install_cargo_sweep(dry_run: bool) -> None:
    log_section("Step 5: Install cargo-sweep")

    if command_exists("cargo-sweep"):
        log("cargo-sweep already installed")
        return

    if dry_run:
        log("[DRY RUN] Would install cargo-sweep via cargo install")
        return

    log("Installing cargo-sweep...")
    result = run(["cargo", "install", "cargo-sweep", "--locked"], check=False)
    if result.returncode != 0:
        log(f"WARNING: cargo-sweep install failed: {result.stderr}")
    else:
        log("cargo-sweep installed")


# ---------------------------------------------------------------------------
# Step 6: Create/update ~/.cargo/config.toml
# ---------------------------------------------------------------------------


def create_global_cargo_config(dry_run: bool) -> None:
    log_section("Step 6: Create ~/.cargo/config.toml")

    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    config_path = cargo_home / "config.toml"
    content = GLOBAL_CARGO_CONFIG.format(jobs=CARGO_JOBS)

    if config_path.exists():
        existing = config_path.read_text()
        if existing == content:
            log("~/.cargo/config.toml already has correct content")
            return
        if dry_run:
            log(f"[DRY RUN] Would overwrite {config_path}")
            log(f"  Current content:\n{existing}")
            return
        # Back up existing
        backup = config_path.with_suffix(".toml.bak")
        config_path.rename(backup)
        log(f"Backed up existing config to {backup}")

    if dry_run:
        log(f"[DRY RUN] Would create {config_path} with:")
        for line in content.splitlines():
            log(f"  {line}")
        return

    cargo_home.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content)
    log(f"Created {config_path}")


# ---------------------------------------------------------------------------
# Step 7: Patch per-project .cargo/config.toml files
# ---------------------------------------------------------------------------


def patch_project_configs(dry_run: bool) -> None:
    log_section("Step 7: Patch per-project .cargo/config.toml files")

    for proj_name in PROJECTS_WITH_JOBS:
        if proj_name in SKIP_PROJECTS:
            log(f"  {proj_name}: SKIPPED (in skip list)")
            continue

        config_path = PROJECTS_DIR / proj_name / ".cargo" / "config.toml"
        if not config_path.exists():
            log(f"  {proj_name}: no .cargo/config.toml — skipping")
            continue

        content = config_path.read_text()
        changed = False

        # Remove jobs = N lines
        new_lines = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("jobs") and "=" in stripped:
                # Match jobs = 2, jobs = 4, jobs=2, etc.
                log(f"  {proj_name}: Removing '{stripped}'")
                changed = True
                continue
            new_lines.append(line)
        content = "\n".join(new_lines)

        # Add mold linker flag to x86_64 rustflags if this project has them
        if proj_name in PROJECTS_WITH_X86_RUSTFLAGS:
            # Check for uncommented mold flag (ignore commented lines)
            has_mold = any(
                "link-arg=-fuse-ld=mold" in line
                for line in content.splitlines()
                if not line.strip().startswith("#")
            )
            if not has_mold:
                # Find the x86_64 rustflags line and append mold flag
                patched_lines = []
                in_x86_section = False
                for line in content.splitlines():
                    stripped = line.strip()
                    # Track when we enter [target.x86_64-unknown-linux-gnu]
                    if stripped.startswith("[target.x86_64"):
                        in_x86_section = True
                    elif stripped.startswith("[") and in_x86_section:
                        in_x86_section = False

                    if in_x86_section and "rustflags" in line and "]" in line:
                        # Append mold flag before the closing bracket
                        line = line.rstrip().rstrip("]").rstrip()
                        line = f'{line}, "-C", "link-arg=-fuse-ld=mold"]'
                        log(
                            f"  {proj_name}: Added mold linker flag to x86_64 rustflags"
                        )
                        changed = True

                    patched_lines.append(line)
                content = "\n".join(patched_lines)

        # Clean up empty lines left behind
        while "\n\n\n" in content:
            content = content.replace("\n\n\n", "\n\n")

        # Ensure trailing newline
        if not content.endswith("\n"):
            content += "\n"

        if not changed:
            log(f"  {proj_name}: No changes needed")
            continue

        if dry_run:
            log(f"  {proj_name}: [DRY RUN] Would update .cargo/config.toml")
            continue

        config_path.write_text(content)
        log(f"  {proj_name}: Updated .cargo/config.toml")


# ---------------------------------------------------------------------------
# Step 8: Clean up old shared target dir
# ---------------------------------------------------------------------------


def cleanup_old_target(dry_run: bool) -> None:
    log_section("Step 8: Clean up old shared target directory")

    if not OLD_SHARED_TARGET.exists():
        log(f"{OLD_SHARED_TARGET} does not exist — already clean")
        return

    # Calculate size
    result = run(["du", "-sh", str(OLD_SHARED_TARGET)], check=False)
    size = result.stdout.split()[0] if result.returncode == 0 else "unknown"
    log(f"Old shared target: {OLD_SHARED_TARGET} ({size})")

    if dry_run:
        log(f"[DRY RUN] Would remove {OLD_SHARED_TARGET} (recovering {size})")
        return

    log(f"Removing {OLD_SHARED_TARGET} ({size})...")
    shutil.rmtree(OLD_SHARED_TARGET)
    log("Removed old shared target directory")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify(dry_run: bool) -> None:
    log_section("Verification")

    if dry_run:
        log("[DRY RUN] Skipping verification")
        return

    # Check env var is gone
    if os.environ.get("CARGO_TARGET_DIR"):
        log("WARNING: CARGO_TARGET_DIR still set in current shell")
        log("  Re-login or run: unset CARGO_TARGET_DIR")
    else:
        log("CARGO_TARGET_DIR: not set (correct)")

    # Check symlinks
    sample = PROJECTS_DIR / "dfe-receiver" / "target"
    if sample.is_symlink():
        log(f"Symlink check: {sample} -> {sample.resolve()}")
    else:
        log(f"WARNING: {sample} is not a symlink")

    # Check sccache
    if command_exists("sccache"):
        result = run(["sccache", "--show-stats"], check=False)
        log(
            f"sccache: installed ({result.stdout.splitlines()[0] if result.returncode == 0 else 'stats unavailable'})"
        )
    else:
        log("WARNING: sccache not found in PATH")

    # Check mold
    if command_exists("mold"):
        result = run(["mold", "--version"], check=False)
        log(f"mold: {result.stdout.strip()}")
    else:
        log("WARNING: mold not found")

    # Check global cargo config
    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    config_path = cargo_home / "config.toml"
    if config_path.exists():
        log(f"Global cargo config: {config_path} exists")
    else:
        log(f"WARNING: {config_path} does not exist")

    log(
        "\nDone. Test concurrent builds by running cargo build in two projects simultaneously."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set up Rust build optimisation on a developer workstation",
        epilog="See docs/RUST-BUILD-OPTIMISATION.md for details.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: show what would change without modifying anything",
    )
    parser.add_argument(
        "--symlinks",
        action="store_true",
        help="Only create per-project target symlinks (skip tool installs)",
    )
    args = parser.parse_args()
    dry_run = args.check

    if dry_run:
        print("\n  DRY RUN MODE — no changes will be made\n")

    # Verify we're on a machine with /cache
    if not CACHE_DIR.exists():
        print(
            f"ERROR: {CACHE_DIR} does not exist. This script is for machines with a /cache disk."
        )
        sys.exit(1)

    # Verify cargo is available
    if not command_exists("cargo"):
        print("ERROR: cargo not found. Install Rust first.")
        sys.exit(1)

    remove_cargo_target_dir_env(dry_run)
    create_target_symlinks(dry_run)

    if not args.symlinks:
        install_sccache(dry_run)
        install_mold(dry_run)
        install_cargo_sweep(dry_run)
        create_global_cargo_config(dry_run)
        patch_project_configs(dry_run)
        cleanup_old_target(dry_run)

    verify(dry_run)


if __name__ == "__main__":
    main()
