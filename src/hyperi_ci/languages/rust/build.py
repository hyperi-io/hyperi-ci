# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/build.py
# Purpose:   Rust build handler with cross-compilation support
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust build handler.

Builds Rust projects in release mode with optional cross-compilation.
For cross-targets with C/C++ dependencies, builds a private sysroot from
downloaded .deb packages (ported from old CI's proven sysroot approach).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from hyperi_ci.common import (
    error,
    group,
    info,
    is_linux,
    is_macos,
    sanitize_ref_name,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust.optimize import (
    OptimizationProfile,
    log_profile,
    parse_cargo_features,
    resolve_optimization_profile,
    validate_profile,
)

_TARGET_MAP = {
    "x86_64-unknown-linux-gnu": ("linux", "amd64"),
    "aarch64-unknown-linux-gnu": ("linux", "arm64"),
    "x86_64-apple-darwin": ("darwin", "amd64"),
    "aarch64-apple-darwin": ("darwin", "arm64"),
    "x86_64-pc-windows-msvc": ("windows", "amd64"),
}

_CROSS_TOOLCHAIN = {
    "aarch64-unknown-linux-gnu": {
        "arch": "arm64",
        "triple": "aarch64-linux-gnu",
        "cc": "aarch64-linux-gnu-gcc",
        "cxx": "aarch64-linux-gnu-g++",
        "ar": "aarch64-linux-gnu-ar",
    },
}


def _sysroot_base() -> Path:
    """Return the base directory for cross-compilation sysroots.

    Uses .tmp/cross-sysroot in the workspace so it stays on the workspace
    volume (not pod ephemeral storage) and follows the .tmp/ convention.
    """
    return Path.cwd() / ".tmp" / "cross-sysroot"


def _get_native_target() -> str:
    """Get the native Rust target triple for this platform."""
    if sys.platform == "darwin":
        import platform

        arch = platform.machine()
        return "aarch64-apple-darwin" if arch == "arm64" else "x86_64-apple-darwin"
    return "x86_64-unknown-linux-gnu"


def _get_native_triple() -> str:
    """Get the native GNU triple (e.g. x86_64-linux-gnu)."""
    result = subprocess.run(
        ["gcc", "-dumpmachine"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "x86_64-linux-gnu"


def _widen_custom_repos_for_arch(arch: str) -> bool:
    """Add cross arch to custom APT repo .list files.

    Custom repos (e.g., Confluent for librdkafka) are initially scoped to the
    native arch (arch=amd64). When cross-compiling, the sysroot builder needs
    to download cross-arch packages from these repos too. This widens the
    arch constraint to include the cross arch.

    Only touches .list files with explicit arch= constraints; skips Ubuntu
    defaults (ubuntu.sources, arm64-ports.list).

    Returns True if any repos were widened (caller should apt-get update).
    """
    sources_dir = Path("/etc/apt/sources.list.d")
    if not sources_dir.exists():
        return False

    widened = False
    skip_files = {"arm64-ports.list", "ubuntu.sources"}
    for list_file in sources_dir.glob("*.list"):
        if list_file.name in skip_files:
            continue
        content = list_file.read_text()
        # Match arch=<archs> in deb lines, e.g. arch=amd64
        pattern = r"(arch=)([a-z0-9,]+)"
        match = re.search(pattern, content)
        if not match:
            continue
        current_archs = match.group(2).split(",")
        if arch in current_archs:
            continue
        new_archs = ",".join(current_archs + [arch])
        new_content = re.sub(pattern, rf"\g<1>{new_archs}", content)
        subprocess.run(
            ["sudo", "tee", str(list_file)],
            input=new_content.encode(),
            capture_output=True,
            check=False,
        )
        info(f"  Widened {list_file.name} arch to include {arch}")
        widened = True
    return widened


def _ensure_cross_apt_metadata(arch: str) -> None:
    """Ensure apt knows about the cross architecture (metadata only).

    On ARC runners the arm64 ports sources are pre-baked into the image.
    On ubuntu-latest they are not, so we add them here if missing.
    We also scope existing deb822 sources to amd64 to prevent apt from
    trying to fetch arm64 from archive.ubuntu.com (which doesn't serve it).
    """
    result = subprocess.run(
        ["dpkg", "--print-foreign-architectures"],
        capture_output=True,
        text=True,
    )
    arch_registered = arch in result.stdout

    if not arch_registered:
        info(f"  Adding apt architecture: {arch}")
        subprocess.run(["sudo", "dpkg", "--add-architecture", arch], check=False)

        ports_list = Path("/etc/apt/sources.list.d/arm64-ports.list")
        if arch == "arm64" and not ports_list.exists():
            info("  Adding arm64 apt sources from ports.ubuntu.com")
            codename_result = subprocess.run(
                ["lsb_release", "-cs"],
                capture_output=True,
                text=True,
            )
            codename = codename_result.stdout.strip() or "noble"
            lines = [
                f"deb [arch=arm64] http://ports.ubuntu.com/ubuntu-ports {codename} main restricted universe",
                f"deb [arch=arm64] http://ports.ubuntu.com/ubuntu-ports {codename}-updates main restricted universe",
                f"deb [arch=arm64] http://ports.ubuntu.com/ubuntu-ports {codename}-security main restricted universe",
            ]
            subprocess.run(
                ["sudo", "tee", str(ports_list)],
                input="\n".join(lines) + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
            deb822_sources = Path("/etc/apt/sources.list.d/ubuntu.sources")
            if deb822_sources.exists():
                content = deb822_sources.read_text()
                if "Architectures:" not in content:
                    info("  Scoping deb822 sources to amd64")
                    subprocess.run(
                        [
                            "sudo",
                            "sed",
                            "-i",
                            "/^Types:/a Architectures: amd64",
                            str(deb822_sources),
                        ],
                        check=False,
                    )

    # Widen custom APT repos (e.g., Confluent) to also serve the cross arch.
    # _add_apt_repo in native_deps.py scopes repos to arch=<native> which
    # prevents apt from finding cross-arch packages for the sysroot.
    repos_widened = _widen_custom_repos_for_arch(arch)

    # Update apt cache when: arch newly registered, few package lists, or
    # custom repos were widened (need cross-arch metadata from those repos).
    apt_lists = Path("/var/lib/apt/lists")
    needs_update = (
        not arch_registered
        or repos_widened
        or len(list(apt_lists.glob("*_Packages"))) < 5
    )
    if needs_update:
        info("  Updating apt package cache...")
        subprocess.run(
            ["sudo", "apt-get", "update", "-qq"],
            capture_output=True,
            check=False,
        )


def _detect_native_dev_packages(native_triple: str) -> list[str]:
    """Auto-detect native -dev packages that provide pkg-config files.

    Scans dpkg for packages that own .pc files under the native triple's
    pkgconfig directory. These are the packages we need cross-arch equivalents for.
    """
    result = subprocess.run(
        ["dpkg", "-S", f"/usr/lib/{native_triple}/pkgconfig/*.pc"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    packages: set[str] = set()
    native_arch = subprocess.run(
        ["dpkg", "--print-architecture"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        pkg_part = line.split(":")[0].strip()
        pkg_part = pkg_part.removesuffix(f":{native_arch}")
        if pkg_part:
            packages.add(pkg_part)

    return sorted(packages)


def _resolve_cross_packages(
    dev_pkgs: list[str],
    cross_arch: str,
) -> list[str]:
    """Resolve cross-arch packages and their transitive lib dependencies.

    Breadth-first walk of dependency tree, limited to lib* packages,
    with max depth of 20 to handle deep chains like:
    libsasl2-dev -> libsasl2-2 -> libssl3t64 (provides libcrypto.so.3)
    """
    seen: set[str] = set()
    to_download: list[str] = []

    pending: list[str] = []
    for pkg in dev_pkgs:
        cross_pkg = f"{pkg}:{cross_arch}"
        result = subprocess.run(
            ["apt-cache", "show", cross_pkg],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            pending.append(cross_pkg)
            seen.add(cross_pkg)

    max_depth = 20
    for depth in range(max_depth):
        if not pending:
            break
        next_pending: list[str] = []
        for pkg in pending:
            to_download.append(pkg)
            result = subprocess.run(
                ["apt-cache", "depends", pkg],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line.startswith("Depends:"):
                    continue
                dep = line.split(":", 1)[1].strip()
                # Keep library packages — most start with lib*, but some
                # (zlib1g) don't. Filter out non-library packages.
                dep_name = dep.split(":")[0]
                if not dep_name.startswith(("lib", "zlib")):
                    continue
                if ":" not in dep:
                    dep = f"{dep}:{cross_arch}"
                if dep in seen:
                    continue
                seen.add(dep)
                check = subprocess.run(
                    ["apt-cache", "show", dep],
                    capture_output=True,
                    check=False,
                )
                if check.returncode == 0:
                    next_pending.append(dep)
        pending = next_pending

        if depth == max_depth - 1:
            warn(f"  Hit max dependency depth ({max_depth}) — some deps may be missing")

    return sorted(set(to_download))


def _patch_ld_scripts(sysroot: Path, cross_triple: str) -> int:
    """Fix absolute paths in GNU LD scripts to point at sysroot.

    Some .so files are ASCII linker scripts like:
      GROUP ( /lib/aarch64-linux-gnu/libm.so.6 ... )
    These absolute paths don't exist on the host — rewrite to sysroot paths.
    """
    lib_dir = sysroot / "usr" / "lib" / cross_triple
    if not lib_dir.exists():
        return 0

    patched = 0
    for so_file in lib_dir.glob("lib*.so"):
        if not so_file.is_file():
            continue
        try:
            content = so_file.read_text()
        except UnicodeDecodeError:
            continue

        original = content
        content = content.replace(
            f" /lib/{cross_triple}/",
            f" {sysroot}/usr/lib/{cross_triple}/",
        )
        content = content.replace(
            f" /usr/lib/{cross_triple}/",
            f" {sysroot}/usr/lib/{cross_triple}/",
        )
        if content != original:
            so_file.write_text(content)
            patched += 1

    return patched


def _apply_usrmerge(sysroot: Path) -> None:
    """Merge /lib into /usr/lib with symlink (matches Ubuntu usrmerge)."""
    lib_dir = sysroot / "lib"
    usr_lib = sysroot / "usr" / "lib"

    if lib_dir.is_dir() and not lib_dir.is_symlink():
        usr_lib.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["cp", "-a"] + [str(p) for p in lib_dir.iterdir()] + [str(usr_lib) + "/"],
            check=False,
        )
        shutil.rmtree(lib_dir)
        lib_dir.symlink_to("usr/lib")
        info("  Applied usrmerge: lib/ merged into usr/lib/ with symlink")
    elif not lib_dir.exists():
        usr_lib.mkdir(parents=True, exist_ok=True)
        lib_dir.symlink_to("usr/lib")


def _setup_cross_sysroot(cross_arch: str, cross_triple: str) -> Path | None:
    """Build private sysroot with cross-arch -dev libraries from .deb packages.

    Ported from old CI's setup_cross_sysroot(). Auto-detects native -dev packages
    with pkg-config files, downloads their cross-arch equivalents + transitive
    library dependencies, and extracts to a private directory. This avoids installing
    cross-arch packages system-wide which can conflict with native packages.

    Returns sysroot path on success, None on failure.
    """
    sysroot = _sysroot_base() / cross_arch

    # Reuse existing sysroot if already populated
    pc_dir = sysroot / "usr" / "lib" / cross_triple / "pkgconfig"
    if pc_dir.exists():
        pc_count = len(list(pc_dir.glob("*.pc")))
        if pc_count > 0:
            info(f"  Cross sysroot already populated ({pc_count} .pc files): {sysroot}")
            return sysroot

    info(f"  Building cross-compilation sysroot ({cross_arch})...")
    info(f"  Libraries will be extracted to {sysroot} (no system installs)")

    _ensure_cross_apt_metadata(cross_arch)

    native_triple = _get_native_triple()
    dev_pkgs = _detect_native_dev_packages(native_triple)

    if not dev_pkgs:
        info("  No native -dev packages with pkg-config files found — skipping sysroot")
        return None

    info(f"  Detected {len(dev_pkgs)} native -dev packages with .pc files:")
    for pkg in dev_pkgs:
        info(f"    {pkg}")

    cross_pkgs = _resolve_cross_packages(dev_pkgs, cross_arch)

    if not cross_pkgs:
        info("  No cross-arch packages available — skipping sysroot")
        return None

    info(f"  Downloading {len(cross_pkgs)} cross-arch packages...")

    deb_dir = sysroot / "_debs"
    deb_dir.mkdir(parents=True, exist_ok=True)

    for pkg in cross_pkgs:
        result = subprocess.run(
            ["apt-get", "download", pkg],
            cwd=str(deb_dir),
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            info(f"    OK: {pkg}")
        else:
            warn(f"    SKIP: {pkg} (not available)")

    debs = list(deb_dir.glob("*.deb"))
    if not debs:
        warn("  No .deb packages downloaded — sysroot will be empty")
        return None

    info(f"  Extracting {len(debs)} packages to {sysroot}/")
    for deb in debs:
        subprocess.run(
            ["dpkg-deb", "-x", str(deb), str(sysroot) + "/"],
            check=False,
        )

    _apply_usrmerge(sysroot)

    patched = _patch_ld_scripts(sysroot, cross_triple)
    if patched > 0:
        info(f"  Patched {patched} GNU LD scripts with sysroot paths")

    lib_dir = sysroot / "usr" / "lib" / cross_triple
    pc_count = len(list(pc_dir.glob("*.pc"))) if pc_dir.exists() else 0
    so_count = len(list(lib_dir.glob("*.so*"))) if lib_dir.exists() else 0
    info(f"  Sysroot ready: {pc_count} pkg-config files, {so_count} shared libraries")

    return sysroot


def _create_linker_wrapper(sysroot: Path, cross_triple: str) -> Path:
    """Create a linker wrapper that injects sysroot library paths.

    The cross-linker doesn't know about our private sysroot. Some -sys crate
    build scripts emit cargo:rustc-link-lib without a search path (e.g. rdkafka-sys
    builds librdkafka via cmake, then emits -lsasl2 without -L). The wrapper adds
    -L flags for the sysroot. -rpath-link is needed so the linker can resolve
    transitive .so dependencies (e.g. libsasl2.so needs libcrypto.so.3).
    """
    wrapper_dir = sysroot / "bin"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    perms = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH

    wrapper_script = """\
#!/bin/sh
exec {real_bin} \\
    -fuse-ld=bfd \\
    -L{sysroot}/usr/lib/{triple} \\
    -L{sysroot}/lib/{triple} \\
    -Wl,-rpath-link,{sysroot}/usr/lib/{triple} \\
    "$@"
"""

    for suffix in ("gcc", "g++"):
        real_bin = (
            shutil.which(f"{cross_triple}-{suffix}")
            or f"/usr/bin/{cross_triple}-{suffix}"
        )
        wrapper = wrapper_dir / f"{cross_triple}-{suffix}"
        wrapper.write_text(
            wrapper_script.format(
                real_bin=real_bin,
                sysroot=sysroot,
                triple=cross_triple,
            )
        )
        wrapper.chmod(perms)
        info(f"  Linker wrapper: {wrapper} -> {real_bin}")

    return wrapper_dir / f"{cross_triple}-gcc"


def _cross_env(target: str, sysroot: Path | None = None) -> dict[str, str]:
    """Build environment variables for cross-compiling C/C++ deps.

    When a sysroot is available, creates a linker wrapper and sets all necessary
    env vars for the cross-compilation toolchain. Follows the old CI's proven
    pattern (build.sh:configure_cross_sysroot_env + install_cross_toolchain).
    """
    toolchain = _CROSS_TOOLCHAIN.get(target)
    if not toolchain:
        return {}

    cross_triple = toolchain["triple"]
    target_upper = target.replace("-", "_").upper()
    # cc crate uses lowercase with underscores for target-specific CFLAGS
    target_lower = target.replace("-", "_")
    env: dict[str, str] = {}

    cc = toolchain["cc"]
    if not shutil.which(cc):
        warn(f"  Cross-compiler {cc} not found — build may fail")
        return env

    # cc crate uses lowercase target with underscores: CC_aarch64_unknown_linux_gnu
    # Cargo uses uppercase for its own vars: CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER
    #
    # IMPORTANT: CC must be the PLAIN cross-compiler, NOT the sysroot wrapper.
    # cmake's TryCompile uses CC to test compiler functionality. If CC is a wrapper
    # that adds linker-specific flags (-L, -Wl,-rpath-link), TryCompile may fail
    # and cmake silently falls back to the host compiler (compiling x86_64 objects
    # for an aarch64 build). The wrapper is used ONLY as the Rust linker.
    env[f"CC_{target_lower}"] = cc
    env[f"CXX_{target_lower}"] = toolchain["cxx"]
    env[f"AR_{target_lower}"] = toolchain["ar"]

    # Configure-based -sys crates (e.g. rdkafka-sys when cmake-build feature is
    # absent) run `./configure && make` and read CC/CXX/AR directly from the
    # environment — they do NOT use the cc crate's CC_<target> lookup.
    # Without CC set, ./configure uses the host gcc (x86_64) even when building
    # for aarch64, producing x86_64 objects that fail at link time with EM:62.
    env["CC"] = cc
    env["CXX"] = toolchain["cxx"]
    env["AR"] = toolchain["ar"]

    if sysroot:
        # Use linker wrapper that injects sysroot paths + forces BFD linker.
        # CARGO_TARGET_..._LINKER sets the Rust linker (not the C compiler).
        wrapper = _create_linker_wrapper(sysroot, cross_triple)
        env[f"CARGO_TARGET_{target_upper}_LINKER"] = str(wrapper)

        # pkg-config paths for the sysroot
        env["PKG_CONFIG_PATH"] = (
            f"{sysroot}/usr/lib/{cross_triple}/pkgconfig:{sysroot}/usr/share/pkgconfig"
        )
        env["PKG_CONFIG_SYSROOT_DIR"] = str(sysroot)
        env["PKG_CONFIG_ALLOW_CROSS"] = "1"

        # cmake-based -sys crates (e.g. rdkafka-sys).
        # Explicitly set CMAKE_C/CXX_COMPILER so cmake uses the cross-compiler
        # even when cmake-rs's cc crate detection fails. This is belt-and-suspenders
        # alongside CC_<target> — cmake checks CMAKE_C_COMPILER before CC.
        env["CMAKE_C_COMPILER"] = cc
        env["CMAKE_CXX_COMPILER"] = toolchain["cxx"]
        env["CMAKE_PREFIX_PATH"] = f"{sysroot}/usr"
        # CMAKE_INCLUDE_PATH ensures cmake finds headers (e.g. curl/curl.h)
        # in the sysroot's architecture-independent include dir
        env["CMAKE_INCLUDE_PATH"] = f"{sysroot}/usr/include"

        # Target-specific CFLAGS/CXXFLAGS for the cc crate.
        # The cc crate passes CFLAGS_<target> to cmake as CMAKE_C_FLAGS, which
        # cmake uses for both compile and link steps (including TryCompile).
        # -fuse-ld=bfd: force GNU BFD linker (mold can't cross-compile aarch64)
        # -I flags: sysroot headers (both arch-independent and arch-specific)
        sysroot_include = sysroot / "usr" / "include"
        sysroot_arch_include = sysroot_include / cross_triple
        cross_cflags = f"-fuse-ld=bfd -I{sysroot_include}"
        if sysroot_arch_include.exists():
            cross_cflags += f" -I{sysroot_arch_include}"
        env[f"CFLAGS_{target_lower}"] = cross_cflags
        env[f"CXXFLAGS_{target_lower}"] = cross_cflags
    else:
        # No sysroot — basic cross-compilation (pure Rust or simple C deps)
        env[f"CARGO_TARGET_{target_upper}_LINKER"] = cc
        env["PKG_CONFIG_ALLOW_CROSS"] = "1"
        env["PKG_CONFIG_SYSROOT_DIR"] = f"/usr/{cross_triple}"

    # Clear host compiler/linker flags to prevent e.g. -fuse-ld=mold
    # from leaking into cmake's CMAKE_EXE_LINKER_FLAGS_INIT
    env["LDFLAGS"] = ""
    env["CFLAGS"] = ""
    env["CXXFLAGS"] = ""

    info(f"  Cross-compilation toolchain: {cc}")
    return env


def _ensure_target_installed(target: str) -> bool:
    """Ensure a Rust target is installed via rustup."""
    native = _get_native_target()
    if target == native:
        return True

    result = subprocess.run(
        ["rustup", "target", "add", target],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error(f"  Failed to install target {target}: {result.stderr.strip()}")
        return False
    info(f"  Installed Rust target: {target}")
    return True


def _ensure_cross_toolchain(target: str) -> None:
    """Install cross-compilation toolchain packages if needed.

    Only installs cross-compilers system-wide (they ARE Multi-Arch safe).
    All -dev libraries go into a private sysroot via _setup_cross_sysroot().
    """
    toolchain = _CROSS_TOOLCHAIN.get(target)
    if not toolchain or not is_linux():
        return

    cross_arch = toolchain["arch"]
    cc = toolchain["cc"]
    cxx = toolchain["cxx"]

    packages: list[str] = []
    if not shutil.which(cc):
        packages.append(f"gcc-{toolchain['triple']}")
    if not shutil.which(cxx):
        packages.append(f"g++-{toolchain['triple']}")

    # libc6-dev provides dynamic linker + standard libs for cross-arch
    result = subprocess.run(
        ["dpkg", "-s", f"libc6-dev:{cross_arch}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        _ensure_cross_apt_metadata(cross_arch)
        packages.append(f"libc6-dev:{cross_arch}")

    if packages:
        info(f"  Installing cross-compilation packages: {' '.join(packages)}")
        subprocess.run(
            ["sudo", "apt-get", "install", "-y", "-qq"] + packages,
            check=False,
        )


_ELF_MACHINE_MAP = {
    "x86_64": "Advanced Micro Devices X86-64",
    "aarch64": "AArch64",
    "i686": "Intel 80386",
    "armv7": "ARM",
    "riscv64": "RISC-V",
}


def _human_size(size: int) -> str:
    """Convert bytes to human-readable size."""
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size}{unit}"
        size //= 1024
    return f"{size}T"


def _target_to_elf_machine(target: str) -> str | None:
    """Map Rust target to expected ELF machine string from readelf -h."""
    for prefix, machine in _ELF_MACHINE_MAP.items():
        if target.startswith(prefix):
            return machine
    return None


def _verify_binary(binary: Path, target: str, native_target: str) -> bool:
    """Post-build binary verification.

    Checks: file exists, minimum size, correct ELF machine type, dynamic deps.
    For native targets: runs --version smoke test.
    """
    errors = 0
    info("    --- Post-build verification ---")

    if not binary.exists():
        error(f"    Binary not found: {binary}")
        return False

    size = binary.stat().st_size
    if size < 102400:
        error(f"    Binary too small ({size} bytes) — likely corrupt")
        errors += 1
    else:
        info(f"    OK: Size {_human_size(size)}")

    if shutil.which("file"):
        result = subprocess.run(
            ["file", str(binary)], capture_output=True, text=True, check=False
        )
        if "ELF" not in result.stdout:
            error(f"    Not an ELF binary: {result.stdout.strip()}")
            errors += 1
        else:
            info("    OK: ELF binary confirmed")

    if shutil.which("readelf"):
        expected = _target_to_elf_machine(target)
        if expected:
            result = subprocess.run(
                ["readelf", "-h", str(binary)],
                capture_output=True,
                text=True,
                check=False,
            )
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

        result = subprocess.run(
            ["readelf", "-d", str(binary)],
            capture_output=True,
            text=True,
            check=False,
        )
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

    if target == native_target:
        for flag in ("--version", "--help"):
            try:
                result = subprocess.run(
                    [str(binary), flag],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                if result.returncode == 0:
                    first_line = result.stdout.splitlines()[0] if result.stdout else ""
                    info(f"    OK: Smoke test ({flag}): {first_line}")
                    break
            except subprocess.TimeoutExpired:
                continue
        else:
            info("    SKIP: Smoke test (binary needs runtime config)")
    else:
        info("    SKIP: Smoke test (cross-compiled, cannot execute)")

    if errors:
        error(f"    {errors} verification failure(s)")
        return False

    info("    All checks passed")
    return True


def _strip_binary(binary: Path, target: str) -> None:
    """Strip debug symbols from binary."""
    strip_cmd = None
    if target.startswith("x86_64-unknown-linux") or target.startswith("x86_64-apple"):
        strip_cmd = "strip"
    elif target.startswith("aarch64-unknown-linux"):
        strip_cmd = "aarch64-linux-gnu-strip"
    elif target.startswith("aarch64-apple"):
        strip_cmd = "strip"

    if strip_cmd and shutil.which(strip_cmd):
        size_before = binary.stat().st_size
        subprocess.run([strip_cmd, str(binary)], check=False)
        size_after = binary.stat().st_size
        saved = _human_size(size_before - size_after)
        info(
            f"    Stripped: {_human_size(size_before)} -> {_human_size(size_after)} (saved {saved})"
        )


def _resolve_build_channel(config: CIConfig) -> str:
    """Resolve the publish channel for build-time optimisation gating.

    Priority (highest wins):
      1. `HYPERCI_CHANNEL` env var (set by reusable workflow when
         `inputs.tag` is non-empty, i.e. `hyperi-ci release` dispatch)
      2. Tag-ref inference: `GITHUB_REF_TYPE == "tag"` → "release"
      3. `RUST_VERSION` / `CI_COMMIT_TAG` env vars (semantic-release-
         style tagged builds set these when checking out the tag)
      4. "spike" (default for push-event CI)

    **Rationale for not falling back to `publish.channel`:** Tier 2
    (PGO + BOLT) adds 30-60 min per build and a bad workload causes
    NEGATIVE gains. It MUST only run on explicit release dispatches
    (artifact publishing), NOT on every push to main. `publish.channel`
    in `.hyperi-ci.yaml` describes *where artifacts are published*
    (JFrog, GHCR, PyPI, etc.) — a project that ships to "release"
    still gets push-event CI on every commit, which must NOT trigger
    Tier 2. The build channel is orthogonal to the publish channel.

    An operator who explicitly wants a one-off release-style build on
    a non-tag ref can set `HYPERCI_CHANNEL=release` in the workflow
    dispatch env, or pin the workflow to a tagged ref.
    """
    override = os.environ.get("HYPERCI_CHANNEL", "").strip().lower()
    if override:
        return override

    if os.environ.get("GITHUB_REF_TYPE", "").strip().lower() == "tag":
        return "release"

    for var in ("RUST_VERSION", "CI_COMMIT_TAG"):
        if os.environ.get(var, "").strip():
            return "release"

    return "spike"


def _detect_binary_names() -> list[str]:
    """Detect binary target names from Cargo metadata.

    Returns empty list for library-only crates (no bin targets).
    Only falls back to directory name when cargo metadata itself fails.

    Feature-gated binaries (those with `required-features = [...]` in
    Cargo.toml) are excluded — they are consumer-selected tools (PGO
    drivers, benchmarking harnesses, etc.) not production artifacts,
    and forcing them into the publish path breaks the default build
    when the feature isn't enabled.
    """
    result = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [Path.cwd().name]

    try:
        meta = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [Path.cwd().name]

    names: list[str] = []
    for package in meta.get("packages", []):
        for target in package.get("targets", []):
            if "bin" not in target.get("kind", []):
                continue
            # Skip feature-gated binaries — they are consumer tools
            # (benchmark drivers, PGO workload generators, etc.) that
            # shouldn't be part of the default publish payload.
            if target.get("required-features"):
                continue
            names.append(target["name"])

    # Return empty list for library-only crates — packaging is skipped
    return names


def _detect_version() -> str:
    """Detect project version from VERSION file, env vars, or Cargo.toml.

    Priority: VERSION file (semantic-release) > explicit env > Cargo.toml > "dev".
    GITHUB_REF_NAME is deliberately excluded — during the publish job it is
    the branch name (e.g. "release"), not the tag.
    """
    version_file = Path("VERSION")
    if version_file.exists():
        val = version_file.read_text().strip()
        if val:
            return f"v{val}" if not val.startswith("v") else val

    for var in ("RUST_VERSION", "CI_COMMIT_TAG"):
        val = os.environ.get(var, "")
        if val:
            return sanitize_ref_name(val)

    result = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        try:
            meta = json.loads(result.stdout)
            for package in meta.get("packages", []):
                version = package.get("version", "")
                if version:
                    return f"v{version}"
        except json.JSONDecodeError:
            pass

    return "dev"


def _target_to_os_arch(target: str) -> str:
    """Map Rust target triple to os-arch naming (matches Go convention)."""
    pair = _TARGET_MAP.get(target)
    if pair:
        return f"{pair[0]}-{pair[1]}"
    return target


def _generate_checksums(output_dir: Path) -> None:
    """Generate SHA256 checksums file for all binaries in output directory."""
    checksum_file = output_dir / "checksums.sha256"
    lines: list[str] = []

    for f in sorted(output_dir.iterdir()):
        if f.is_file() and f.name != "checksums.sha256":
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            lines.append(f"{sha}  {f.name}")

    if lines:
        checksum_file.write_text("\n".join(lines) + "\n")
        info(f"Checksums written to {checksum_file}")


def _package_binaries(
    targets: list[str],
    binary_names: list[str],
    version: str,
    native_target: str,
) -> int:
    """Copy, strip, verify, and package built binaries into dist/.

    Creates binaries like: name-linux-amd64
    Version is in the R2/release path, not the filename.
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

            if "windows" in target:
                src_bin = src_bin.with_suffix(".exe")
                output_name += ".exe"

            if not src_bin.exists():
                error(f"Binary not found: {src_bin}")
                return 1

            output_path = output_dir / output_name
            shutil.copy2(src_bin, output_path)
            output_path.chmod(0o755)

            _strip_binary(output_path, target)

            info(
                f"  Created: {output_path.name} ({_human_size(output_path.stat().st_size)})"
            )

            if not _verify_binary(output_path, target, native_target):
                error(f"Post-build verification failed for {output_path}")
                return 1

            built_count += 1

    info(f"Built {built_count} binary(ies) to {output_dir}/")
    for f in sorted(output_dir.iterdir()):
        if f.is_file() and f.name != "checksums.sha256":
            info(f"  {f.name} ({_human_size(f.stat().st_size)})")

    _generate_checksums(output_dir)

    return 0


def _expected_elf_machine(target: str) -> str | None:
    """Return the `file` command arch substring for a Rust target triple.

    Used to detect whether a cached cross-compiled .o file has the correct
    machine architecture, or if it is stale from a native build.
    """
    arch_map = {
        "x86_64": "x86-64",
        "aarch64": "ARM aarch64",
        "armv7": "ARM",
        "i686": "80386",
        "riscv64": "RISC-V",
    }
    for prefix, name in arch_map.items():
        if target.startswith(prefix):
            return name
    return None


def _find_c_sys_crates() -> list[str]:
    """Find all -sys crates from Cargo.lock that may compile C code.

    Returns all package names ending in -sys. These are candidates for
    stale cross-compiled rlibs on persistent ARC runners. Covers both
    cmake-build crates (rdkafka-sys with cmake feature) and configure-based
    crates (rdkafka-sys default build, libz-sys, zstd-sys, aws-lc-sys, etc.)
    """
    lock_path = Path("Cargo.lock")
    if not lock_path.exists():
        return []

    try:
        content = lock_path.read_text()
    except OSError:
        return []

    sys_crates: list[str] = []
    for line in content.splitlines():
        if line.startswith("name = ") and '"' in line:
            name = line.split('"')[1]
            if name.endswith("-sys"):
                sys_crates.append(name)

    return sorted(set(sys_crates))


def _rlib_has_wrong_arch(rlib: Path, expected_arch_substr: str) -> bool:
    """Check if a compiled rlib contains objects with the wrong ELF architecture.

    Extracts the first .o file from the rlib (ar archive) and checks its
    ELF machine type using `file`. Returns True if the arch is wrong.
    """
    ar_cmd = shutil.which("ar")
    file_cmd = shutil.which("file")
    if not ar_cmd or not file_cmd:
        return False

    list_result = subprocess.run(
        [ar_cmd, "t", str(rlib)],
        capture_output=True,
        text=True,
        check=False,
    )
    if list_result.returncode != 0:
        return False

    for obj_name in list_result.stdout.splitlines():
        if not obj_name.endswith(".o"):
            continue
        extract_result = subprocess.run(
            [ar_cmd, "p", str(rlib), obj_name],
            capture_output=True,
            check=False,
        )
        if extract_result.returncode != 0 or not extract_result.stdout:
            continue
        file_result = subprocess.run(
            [file_cmd, "-"],
            input=extract_result.stdout,
            capture_output=True,
            check=False,
        )
        output = file_result.stdout.decode(errors="replace")
        if not output:
            continue
        return expected_arch_substr not in output

    return False


def _clean_stale_sys_crates(target: str) -> None:
    """Clean -sys crate artifacts if they have wrong-arch objects.

    On persistent ARC runners, target/ is NOT cleaned between runs by
    actions/checkout (git clean -ffd omits gitignored files by default).
    If a previous run compiled a -sys crate's C code with the host compiler
    (e.g. rdkafka-sys configure build using host gcc instead of cross-gcc),
    the stale rlib persists. Since build.rs scripts don't declare
    cargo:rerun-if-env-changed for CC, cargo reuses the cached x86_64 rlib
    — causing "Relocations in generic ELF (EM: 62)" on link.

    Covers both cmake-build crates and configure-based crates (rdkafka-sys,
    libz-sys, zstd-sys, aws-lc-sys, etc.) — all -sys crates are scanned.
    """
    expected = _expected_elf_machine(target)
    if not expected:
        return

    target_dir = Path(os.environ.get("CARGO_TARGET_DIR", "target"))
    cross_deps = target_dir / target / "release" / "deps"
    if not cross_deps.exists():
        return

    sys_crates = _find_c_sys_crates()
    if not sys_crates:
        return

    sys_crates_underscored = {p.replace("-", "_") for p in sys_crates}
    to_clean: list[str] = []

    for rlib in cross_deps.glob("lib*.rlib"):
        stem = rlib.stem.lstrip("lib")
        crate_under = stem.split("-")[0] if "-" in stem else stem
        if crate_under not in sys_crates_underscored:
            continue
        if _rlib_has_wrong_arch(rlib, expected):
            pkg_name = crate_under.replace("_", "-")
            to_clean.append(pkg_name)
            warn(f"  Stale cross-compiled rlib detected: {rlib.name} (wrong arch)")

    for pkg in set(to_clean):
        info(f"  Cleaning stale -sys package: {pkg} --target {target}")
        subprocess.run(
            ["cargo", "clean", "--package", pkg, "--target", target],
            check=False,
        )


def _build_for_target(
    target: str,
    features: str,
    all_features: bool,
    extra_env: dict[str, str] | None = None,
    profile: OptimizationProfile | None = None,
) -> int:
    """Build for a specific target triple.

    If `profile` is provided and `profile.pgo_enabled` is True, the PGO
    pipeline (instrument -> workload -> optimise, optionally followed
    by BOLT) is used instead of a plain `cargo build`.

    Otherwise, the profile's allocator features are merged with
    user-supplied `features` and `env_overrides()` (CARGO_PROFILE_RELEASE_LTO)
    are injected into the build environment.
    """
    if not _ensure_target_installed(target):
        return 1

    # Merge profile allocator features with user-supplied features
    if profile:
        profile_features = profile.cargo_features()
        if profile_features:
            user_features = (
                [f for f in features.split(",") if f.strip()] if features else []
            )
            # Deduplicate while preserving order
            seen: set[str] = set()
            merged: list[str] = []
            for f in [*user_features, *profile_features]:
                if f and f not in seen:
                    seen.add(f)
                    merged.append(f)
            features = ",".join(merged)

    # Merge profile env overrides (LTO) into extra_env
    if profile:
        profile_env = profile.env_overrides()
        extra_env = {**(extra_env or {}), **profile_env}

    # PGO path takes over the whole build for this target
    if profile and profile.pgo_enabled:
        binary_names = _detect_binary_names()
        if not binary_names:
            warn(
                "PGO requested but crate has no binaries — falling back to plain build"
            )
        else:
            # Use the first binary for PGO (projects with multiple bins can
            # extend this later; the common case is one binary per crate).
            from hyperi_ci.languages.rust.pgo import run_pgo_build

            return run_pgo_build(
                target=target,
                profile=profile,
                binary_name=binary_names[0],
                cwd=Path.cwd(),
                extra_env=extra_env,
            )

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
    if target != native and is_linux():
        _ensure_cross_toolchain(target)

        toolchain = _CROSS_TOOLCHAIN.get(target)
        sysroot: Path | None = None
        if toolchain:
            sysroot = _setup_cross_sysroot(toolchain["arch"], toolchain["triple"])

        # On persistent runners (ARC), target/ survives between runs.
        # -sys crates that compile C code don't declare
        # cargo:rerun-if-env-changed for CC, so cargo won't detect that a
        # stale x86_64 rlib needs rebuilding with the correct cross-compiler.
        # Detect and clean stale wrong-arch rlibs before building.
        _clean_stale_sys_crates(target)

        env.update(_cross_env(target, sysroot=sysroot))

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

    # Sort targets: native first, then cross targets
    # Avoids Multi-Arch package conflicts (some -dev packages replace each other)
    native = _get_native_target()
    targets.sort(key=lambda t: (0 if t == native else 1, t))

    # Resolve release-track optimisation profile (channel-gated).
    # Libraries (no binaries) skip this entirely — their release profile is
    # irrelevant because consumers recompile from source.
    binary_names_for_profile = _detect_binary_names()
    base_profile: OptimizationProfile | None = None
    if binary_names_for_profile:
        channel = _resolve_build_channel(config)
        user_optimize = config.get("build.rust.optimize") or {}
        base_profile = resolve_optimization_profile(channel, user_optimize)
        cargo_features = parse_cargo_features(Path.cwd() / "Cargo.toml")
        # Target-specific validation happens per-target (BOLT is Linux-only)

    for target in targets:
        with group(f"Build: {target}"):
            profile = None
            if base_profile:
                profile = validate_profile(
                    base_profile,
                    cargo_features=cargo_features,
                    target=target,
                )
                log_profile(profile)
            rc = _build_for_target(
                target, features, all_features, extra, profile=profile
            )
            if rc != 0:
                error(f"Build failed for target: {target}")
                return rc
            success(f"Built: {target}")

    with group("Binary packaging"):
        binary_names = _detect_binary_names()
        if not binary_names:
            info("Library-only crate — skipping binary packaging")
        else:
            version = _detect_version()
            rc = _package_binaries(targets, binary_names, version, native)
            if rc != 0:
                return rc

    success("Build complete")
    return 0
