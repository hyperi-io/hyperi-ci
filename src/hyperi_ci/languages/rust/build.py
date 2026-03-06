# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/build.py
# Purpose:   Rust build handler with cross-compilation support
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust build handler.

Builds Rust projects in release mode with optional cross-compilation.
Sets CC/CXX/PKG_CONFIG environment variables for cross-targets so that
C/C++ dependencies (e.g. librdkafka via cmake-build) compile correctly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from hyperi_ci.common import error, group, info, is_macos, success, warn
from hyperi_ci.config import CIConfig

_TARGET_MAP = {
    "x86_64-unknown-linux-gnu": ("linux", "amd64"),
    "aarch64-unknown-linux-gnu": ("linux", "arm64"),
    "x86_64-apple-darwin": ("darwin", "amd64"),
    "aarch64-apple-darwin": ("darwin", "arm64"),
    "x86_64-pc-windows-msvc": ("windows", "amd64"),
}

_CROSS_TOOLCHAIN = {
    "aarch64-unknown-linux-gnu": {
        "cc": "aarch64-linux-gnu-gcc",
        "cxx": "aarch64-linux-gnu-g++",
        "ar": "aarch64-linux-gnu-ar",
        "linker": "aarch64-linux-gnu-gcc",
        "pkg_config_sysroot": "/usr/aarch64-linux-gnu",
    },
}


def _get_native_target() -> str:
    """Get the native Rust target triple for this platform."""
    if sys.platform == "darwin":
        import platform

        arch = platform.machine()
        return "aarch64-apple-darwin" if arch == "arm64" else "x86_64-apple-darwin"
    return "x86_64-unknown-linux-gnu"


def _cross_env(target: str) -> dict[str, str]:
    """Build environment variables for cross-compiling C/C++ deps."""
    toolchain = _CROSS_TOOLCHAIN.get(target)
    if not toolchain:
        return {}

    target_upper = target.replace("-", "_").upper()
    env: dict[str, str] = {}

    cc = toolchain["cc"]
    if shutil.which(cc):
        env[f"CC_{target_upper}"] = cc
        env[f"CXX_{target_upper}"] = toolchain["cxx"]
        env[f"AR_{target_upper}"] = toolchain["ar"]
        env[f"CARGO_TARGET_{target_upper}_LINKER"] = toolchain["linker"]
        env["PKG_CONFIG_ALLOW_CROSS"] = "1"
        env["PKG_CONFIG_SYSROOT_DIR"] = toolchain["pkg_config_sysroot"]
        info(f"  Cross-compilation toolchain: {cc}")
    else:
        warn(f"  Cross-compiler {cc} not found — build may fail")

    return env


def _build_for_target(
    target: str,
    features: str,
    all_features: bool,
    extra_env: dict[str, str] | None = None,
) -> int:
    """Build for a specific target triple."""
    cmd = ["cargo", "build", "--release", "--target", target]

    if all_features:
        cmd.append("--all-features")
    elif features and features not in ("all", "default"):
        cmd.extend(["--features", features])

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    # Set cross-compilation env vars for C/C++ dependencies
    native = _get_native_target()
    if target != native:
        env.update(_cross_env(target))

    info(f"  Building for {target}...")
    result = subprocess.run(cmd, env=env)
    return result.returncode


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Rust build.

    Args:
        config: Merged CI configuration.
        extra_env: Additional env vars (RUST_BUILD_TARGETS, RUST_FEATURES, etc).

    Returns:
        Exit code (0 = success).
    """
    extra = extra_env or {}
    info("Building Rust project...")

    features = extra.get("RUST_FEATURES", "")
    all_features = extra.get("RUST_ALL_FEATURES", "false") == "true"
    targets_str = extra.get("RUST_BUILD_TARGETS", "")

    if targets_str:
        targets = [t.strip() for t in targets_str.split(",") if t.strip()]
    else:
        targets = [_get_native_target()]

    # On macOS, only build native targets
    if is_macos():
        native = _get_native_target()
        non_native = [t for t in targets if t != native]
        if non_native:
            warn(f"Skipping cross-compile targets on macOS: {', '.join(non_native)}")
        targets = [t for t in targets if t == native]

    for target in targets:
        with group(f"Build: {target}"):
            rc = _build_for_target(target, features, all_features, extra)
            if rc != 0:
                error(f"Build failed for target: {target}")
                return rc
            success(f"Built: {target}")

    return 0
