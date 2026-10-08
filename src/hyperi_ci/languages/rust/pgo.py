# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/pgo.py
# Purpose:   PGO + BOLT build orchestration for Rust binaries
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""PGO + BOLT build orchestration: instrument, run the workload, optimise.

`run_pgo_build()` is the entry point, for when `profile.pgo_enabled` is True.
A cargo-pgo that cannot be installed falls back to a plain release build and
a missing BOLT toolchain keeps the PGO-only binary, but a failed workload is
a hard error: bad profile data is worse than no PGO.
"""

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, warn
from hyperi_ci.languages._build_common import elf_section_names
from hyperi_ci.languages.rust.optimize import (
    OptimizationOutcome,
    OptimizationProfile,
    cargo_feature_args,
)
from hyperi_ci.llvm_version import LLVMVersionError, designated_llvm_version
from hyperi_ci.upgrade import CACHE_DIR
from hyperi_ci.versions import tool_version

# llvm-bolt writes this note into every binary it rewrites, and strip keeps it,
# so it survives packaging and marks a shipped file as BOLT output.
BOLT_NOTE_SECTION = ".note.bolt_info"

# Profile-use compiles skip sccache, whose reply carrying a crate's 27.7 MB of
# missing-profile warnings never arrives, leaving cargo waiting forever (#436).
# Cargo reads an empty RUSTC_WRAPPER as "no wrapper", over any config.
_PROFILE_USE_ENV = {"RUSTC_WRAPPER": ""}

# cargo-pgo applies the BOLT profile to the layout `bolt build` recorded it on,
# so `bolt build` and `bolt optimize` must both carry this flag or neither.
_BOLT_WITH_PGO = "--with-pgo"


def _bolt_profile_env() -> dict[str, str]:
    """Return the cargo profile settings every compile of a PGO + BOLT run carries.

    Cargo hashes profile settings into symbol names, so all compiles sharing a
    PGO profile must match. Strip is off because lld refuses `--strip-all`
    beside `--emit-relocs`; packaging strips the binary afterwards.
    """
    return {"CARGO_PROFILE_RELEASE_STRIP": "none"}


def run_pgo_build(
    target: str,
    profile: OptimizationProfile,
    binary_name: str,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    outcome: OptimizationOutcome | None = None,
    shipped_binaries: list[str] | None = None,
) -> int:
    """Run the PGO (and optionally BOLT) pipeline for one target.

    Args:
        target: Target triple (e.g. "x86_64-unknown-linux-gnu").
        profile: Resolved + validated profile with `pgo_enabled` True.
        binary_name: Binary whose instrumented build the workload runs.
        cwd: Working directory (project root).
        extra_env: Env vars for cargo and the workload, including
                   RUST_FEATURES / RUST_ALL_FEATURES.
        outcome: Filled in with the stages that actually completed.
        shipped_binaries: The binaries each `cargo pgo` step builds; defaults
                          to ``[binary_name]``.

    Returns:
        0 on success, non-zero on failure.

    """
    env_features = extra_env or {}
    feature_args = cargo_feature_args(
        profile,
        env_features.get("RUST_FEATURES", ""),
        all_features=env_features.get("RUST_ALL_FEATURES") == "true",
    )
    cargo_args = [
        *_bin_scope_args(shipped_binaries or [binary_name]),
        *feature_args,
    ]

    if not _ensure_cargo_pgo_installed():
        warn(
            "cargo-pgo unavailable -- falling back to plain release build "
            "(Tier 1 optimisations still apply)"
        )
        return _run_plain_release_build(target, feature_args, cwd, extra_env)

    try:
        designated_llvm_version(cwd)
    except LLVMVersionError as exc:
        error(str(exc))
        return 1

    if not _ensure_ld_lld_available(cwd):
        warn(
            "no ld.lld on PATH -- a project selecting -fuse-ld=lld in its own "
            "cargo config will fail this build with \"cannot find 'ld'\""
        )
    if not _ensure_clang_available(cwd):
        warn(
            "no clang of a known LLVM major to shim -- a project with "
            'linker = "clang" in its own cargo config links with whatever clang '
            'PATH holds, of an unknown major, or fails with "linker `clang` not '
            'found" if there is none'
        )

    # Checked before the workload, or its profiling time is spent for nothing.
    if not _ensure_llvm_profdata_available():
        error(
            "llvm-profdata is unavailable and cargo-pgo cannot merge the "
            "profile without it -- refusing to spend the workload on a PGO "
            "build that cannot finish"
        )
        return 1

    # Decided before the first compile: BOLT's profile settings reach PGO too.
    bolt = profile.bolt_enabled and _ensure_llvm_bolt_available(cwd)
    if profile.bolt_enabled and not bolt:
        warn(
            "BOLT toolchain not complete (llvm-bolt / merge-fdata / ld.lld) -- "
            "skipping BOLT step"
        )
    pipeline_env = {**(extra_env or {}), **_bolt_profile_env()} if bolt else extra_env

    # Later stages link binaries at least as large, so a rescuing linker carries on.
    build_env = pipeline_env

    info(f"PGO: building instrumented binary for {target}")
    instrument_args = ["build", "--", "--target", target, *cargo_args]
    rc = _run_cargo_pgo(instrument_args, cwd=cwd, extra_env=build_env)
    if rc != 0 and target.startswith("aarch64") and shutil.which("mold"):
        # Profile counters push a large binary's text past the +/-128 MB
        # R_AARCH64_CALL26 branch, which bfd cannot bridge; mold inserts thunks.
        warn(
            "PGO instrumented build failed on aarch64 -- retrying once with mold, "
            "which bridges branches past the 128 MB limit"
        )
        build_env = {
            **(pipeline_env or {}),
            _target_rustflags_key(target): "-C link-arg=-fuse-ld=mold",
        }
        rc = _run_cargo_pgo(instrument_args, cwd=cwd, extra_env=build_env)
    if rc != 0:
        error(f"PGO instrumented build failed for {target}")
        return rc

    instrumented_bin = _instrumented_binary_path(
        cwd, target, binary_name, variant="pgo"
    )
    if not instrumented_bin.exists():
        error(f"Instrumented binary not found at {instrumented_bin}")
        return 1

    if profile.pgo_workload_setup_cmd:
        rc = _run_workload_setup(profile.pgo_workload_setup_cmd, cwd)
        if rc != 0:
            error("PGO workload setup failed -- aborting before profiling")
            return rc

    rc = _run_workload(
        profile.pgo_workload_cmd or "",
        profile.pgo_duration_secs,
        instrumented_bin,
        cwd=cwd,
    )
    if rc != 0:
        error("PGO workload failed -- aborting (bad profile data is worse than no PGO)")
        return rc

    info(f"PGO: building optimised binary for {target}")
    rc = _run_cargo_pgo(
        ["optimize", "--", "--target", target, *cargo_args],
        cwd=cwd,
        extra_env={**(build_env or {}), **_PROFILE_USE_ENV},
    )
    if rc != 0:
        error(f"PGO optimised build failed for {target}")
        return rc
    if outcome:
        outcome.pgo_applied = True

    if bolt:
        rc = _run_bolt(
            target, cargo_args, binary_name, profile, cwd, build_env, outcome
        )
        if rc != 0:
            warn("BOLT step failed -- continuing with PGO-only optimised binary")

    return 0


def _bin_scope_args(binaries: list[str]) -> list[str]:
    """Render one ``--bin`` per shipped binary for a `cargo pgo` step.

    Unfiltered, `--all-features` also builds a feature-gated workload driver
    against a profile that does not cover it (#526).
    """
    args: list[str] = []
    for name in binaries:
        args.extend(["--bin", name])
    return args


def _run_plain_release_build(
    target: str,
    feature_args: list[str],
    cwd: Path,
    extra_env: dict[str, str] | None,
) -> int:
    """Run a plain `cargo build --release`, keeping the Tier 1 allocator and LTO."""
    cmd = ["cargo", "build", "--release", "--target", target, *feature_args]
    info(f"  $ {' '.join(cmd)}")
    return run_cmd(cmd, check=False, cwd=cwd, env=extra_env).returncode


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

    The pin (`tools.cargo-pgo`) means the tool that rewrites the release binary
    is the reviewed one. Puts `~/.cargo/bin` on PATH, which some CI runners
    omit. Returns True if cargo-pgo is available afterwards.
    """
    pinned = tool_version("cargo-pgo")
    installed = _installed_cargo_pgo_version()
    if installed == pinned:
        return True

    cargo_bin = Path.home() / ".cargo" / "bin"
    current_path = os.environ.get("PATH", "")
    if str(cargo_bin) not in current_path.split(os.pathsep):
        os.environ["PATH"] = f"{cargo_bin}{os.pathsep}{current_path}"

    install = ["cargo", "install", "cargo-pgo", "--version", pinned, "--locked"]
    found = f"found {installed}" if installed else "not found"
    info(f"cargo-pgo {found}, pinned {pinned} -- installing with '{' '.join(install)}'")
    result = run_cmd(install, check=False)
    if result.returncode != 0:
        warn("cargo-pgo install failed")
        return False

    if shutil.which("cargo-pgo"):
        return True

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


def _ensure_llvm_bolt_available(project_dir: Path | None = None) -> bool:
    """Shim the BOLT toolchain onto PATH unversioned, all from one LLVM major.

    The `bolt-NN` apt package ships only suffixed names, and cargo-pgo calls
    `llvm-bolt` and `merge-fdata` unversioned. Returns True only when every
    binary resolves, since cargo-pgo's BOLT step fails silently on a partial
    toolchain. native_deps.py installs the package.
    """
    return _shim_llvm_tools(_BOLT_TOOLCHAIN_BINARIES, project_dir)


def _rustc_sysroot_bin() -> Path | None:
    """Return the directory `llvm-tools` installs into, or None without rustc."""
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

    The rustc sysroot copy (rustup's `llvm-tools-preview`) is preferred, so
    every arch runs the same binary whatever the runner image has on PATH.
    PATH is the fallback when the component cannot be added.
    """
    bin_dir = _rustc_sysroot_bin()

    if bin_dir is not None and not (bin_dir / "llvm-profdata").exists():
        # run_cmd raises on a missing rustup, which would skip the PATH fallback.
        if shutil.which("rustup"):
            info("  llvm-profdata missing - adding the llvm-tools-preview component")
            run_cmd(["rustup", "component", "add", "llvm-tools-preview"], check=False)
        else:
            info("  llvm-profdata missing and rustup is not on PATH to add it")

    if bin_dir is not None and (bin_dir / "llvm-profdata").exists():
        current = os.environ.get("PATH", "")
        if str(bin_dir) not in current.split(os.pathsep):
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{current}"
        info(f"  llvm-profdata: {bin_dir / 'llvm-profdata'} (rustc sysroot)")
        return True

    found = shutil.which("llvm-profdata")
    if found:
        info(f"  llvm-profdata: {found} (on PATH - no sysroot copy)")
        return True

    warn("  llvm-profdata is not in the rustc sysroot and not on PATH")
    return False


def _ensure_ld_lld_available(project_dir: Path | None = None) -> bool:
    """Put the designated LLVM major's `ld.lld` first on PATH, unversioned.

    gcc's `-fuse-ld=lld` wants exactly `ld.lld`, and the `lld-NN` apt package
    ships only the suffixed name.
    """
    return _shim_llvm_tools(("ld.lld",), project_dir)


def _ensure_clang_available(project_dir: Path | None = None) -> bool:
    """Put the designated LLVM major's `clang` and `clang++` first on PATH.

    Under `-fuse-ld=lld` clang takes `ld.lld` from its own install first, so
    the designated major must own the driver too. Shimmed apart from
    `ld.lld`, so a runner without `clang-NN` still gets the designated lld.
    """
    return _shim_llvm_tools(("clang", "clang++"), project_dir)


# LLVM majors scanned, newest first, when the designated one is incomplete.
_LLVM_FALLBACK_SCAN = range(30, 17, -1)


def _versioned_tools(names: tuple[str, ...], major: int) -> dict[str, str] | None:
    """Return each tool's ``<name>-<major>`` path, or None if any is missing."""
    resolved: dict[str, str] = {}
    for name in names:
        found = shutil.which(f"{name}-{major}")
        if not found:
            return None
        resolved[name] = found
    return resolved


def _prepend_to_path(directory: Path) -> None:
    """Put ``directory`` first on PATH, moving it up if it is already listed."""
    current = os.environ.get("PATH", "")
    entries = current.split(os.pathsep) if current else []
    # An empty entry means the cwd, and Path("") would compare equal to ".".
    kept = [entry for entry in entries if not entry or Path(entry) != directory]
    os.environ["PATH"] = os.pathsep.join([str(directory), *kept])


def _install_shims(resolved: dict[str, str]) -> None:
    """Symlink each tool unversioned into ``~/.local/bin`` and put it first on PATH."""
    shim_dir = Path.home() / ".local" / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    for name, versioned in resolved.items():
        shim = shim_dir / name
        if shim.exists() or shim.is_symlink():
            shim.unlink()
        shim.symlink_to(versioned)
    _prepend_to_path(shim_dir)


def _describe_tools(resolved: dict[str, str]) -> str:
    """Render ``name -> path`` pairs for one log line."""
    return ", ".join(f"{name} -> {path}" for name, path in resolved.items())


def _llvm_major_of(real: Path) -> int | None:
    """Return the LLVM major a resolved tool path belongs to, or None if unknown.

    apt.llvm.org installs into ``/usr/lib/llvm-NN/``, and its ``/usr/bin``
    entries carry a ``-NN`` suffix, so either one names the major.
    """
    for part in reversed(real.parent.parts):
        if match := re.fullmatch(r"llvm-(\d+)", part):
            return int(match.group(1))
    if match := re.search(r"-(\d+)$", real.name):
        return int(match.group(1))
    return None


def _shim_llvm_tools(names: tuple[str, ...], project_dir: Path | None = None) -> bool:
    """Make every tool in ``names`` resolvable unversioned, from ONE LLVM major.

    Order: the designated major's ``<name>-<major>`` shimmed into
    ``~/.local/bin`` first on PATH; else the unversioned tools on PATH if all
    share one known major; else the newest major that provides them all.
    Returns True when all of them resolve.
    """
    designated = designated_llvm_version(project_dir)
    resolved = _versioned_tools(names, designated.major)
    if resolved is not None:
        _install_shims(resolved)
        info(
            f"LLVM {designated.major} ({designated.source}): "
            f"{_describe_tools(resolved)}"
        )
        return True

    missing = ", ".join(
        f"{name}-{designated.major}"
        for name in names
        if not shutil.which(f"{name}-{designated.major}")
    )
    reason = (
        f"designated LLVM {designated.major} ({designated.source}) is "
        f"incomplete, {missing} not found"
    )

    on_path = {name: found for name in names if (found := shutil.which(name))}
    real = {name: Path(path).resolve() for name, path in on_path.items()}
    majors = [_llvm_major_of(path) for path in real.values()]
    # A tool whose major cannot be read is never assumed to match the others.
    one_major = None not in majors and len(set(majors)) == 1
    if len(on_path) == len(names) and one_major:
        major = majors[0]
        described = _describe_tools({name: str(path) for name, path in real.items()})
        warn(f"{reason} -- using the LLVM {major} tools already on PATH instead")
        info(f"LLVM {major} (fallback from {designated.major}, on PATH): {described}")
        return True

    for major in _LLVM_FALLBACK_SCAN:
        if major == designated.major:
            continue
        resolved = _versioned_tools(names, major)
        if resolved is None:
            continue
        _install_shims(resolved)
        warn(f"{reason} -- using LLVM {major} instead")
        info(
            f"LLVM {major} (fallback from {designated.major}): {_describe_tools(resolved)}"
        )
        return True

    return False


def _run_cargo_pgo(
    args: list[str],
    cwd: Path,
    extra_env: dict[str, str] | None,
) -> int:
    """Run `cargo pgo <args>`, with ``extra_env`` (empty values too) over the env."""
    cmd = ["cargo", "pgo", *args]
    info(f"  $ {' '.join(cmd)}")
    return run_cmd(cmd, check=False, cwd=cwd, env=extra_env).returncode


# SECURITY: the workload commands come from the project's own .hyperi-ci.yaml
# and run through a shell; a repo that can edit its config can already run code.
_SHELL = ("/bin/sh", "-c")

# Its own clock: building a load driver can outlast the profiling run.
_WORKLOAD_SETUP_TIMEOUT_SECS = 3600


def _run_workload_setup(setup_cmd: str, cwd: Path) -> int:
    """Run `pgo.workload_setup_cmd` once per target, before the workload clock."""
    info(f"  $ {setup_cmd}  (workload setup, timeout={_WORKLOAD_SETUP_TIMEOUT_SECS}s)")
    try:
        result = run_cmd(
            [*_SHELL, setup_cmd],
            check=False,
            cwd=cwd,
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

    The script contract (see templates/pgo-workload/): the binary path is
    `$1` and also `HYPERCI_PGO_INSTRUMENTED_BINARY`, and
    `PGO_WORKLOAD_DURATION_SECS` says how long to drive it. The script should
    stop itself; the `duration_secs + 600` timeout only catches a hang.
    """
    if not workload_cmd:
        error("PGO enabled but workload_cmd is empty")
        return 1

    env = {
        "HYPERCI_PGO_INSTRUMENTED_BINARY": str(instrumented_binary),
        "PGO_WORKLOAD_DURATION_SECS": str(duration_secs),
    }

    import shlex as _shlex

    full_cmd = f"{workload_cmd} {_shlex.quote(str(instrumented_binary))}"

    timeout_secs = duration_secs + 600
    info(f"  $ {full_cmd}  (timeout={timeout_secs}s = duration+600s safety grace)")
    try:
        result = run_cmd(
            [*_SHELL, full_cmd],
            check=False,
            cwd=cwd,
            env=env,
            timeout=timeout_secs,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        error(
            f"PGO workload exceeded {timeout_secs}s timeout -- "
            "workload should self-terminate at duration_secs"
        )
        return 1


def _release_dir(cwd: Path, target: str) -> Path:
    """Return cargo's `--release --target` output dir, honouring CARGO_TARGET_DIR.

    Packaging resolves it the same way, so every step looks where it copies from.
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
    """Return the instrumented binary's path; BOLT's carries `-bolt-instrumented`."""
    name = f"{binary_name}-bolt-instrumented" if variant == "bolt" else binary_name
    return _release_dir(cwd, target) / name


def _install_bolt_output(cwd: Path, target: str, binary_name: str) -> bool:
    """Copy `<bin>-bolt-optimized` over the unsuffixed name packaging ships.

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


# For the BOLT retry only: llvm-bolt in relocation mode aborts on `<fn>.cold`
# fragments the compiler already split ("parent function not found for
# <fn>.cold"), so the retry stops the compiler splitting. The cl::opt varies by
# toolchain; HYPERCI_BOLT_EXTRA_RUSTFLAGS overrides it, and "" skips the retry.
_DEFAULT_BOLT_NO_SPLIT_RUSTFLAGS = "-Cllvm-args=-hot-cold-split=false"


def _bolt_no_split_rustflags() -> str:
    """Return RUSTFLAGS for the no-split BOLT retry; empty disables the retry."""
    val = os.environ.get("HYPERCI_BOLT_EXTRA_RUSTFLAGS")
    return _DEFAULT_BOLT_NO_SPLIT_RUSTFLAGS if val is None else val.strip()


def _target_rustflags_key(target: str) -> str:
    """Return cargo's env name for `target.<triple>.rustflags`."""
    return (
        f"CARGO_TARGET_{target.upper().replace('-', '_').replace('.', '_')}_RUSTFLAGS"
    )


def _target_linker_key(target: str) -> str:
    """Return cargo's env name for `target.<triple>.linker`."""
    return _target_rustflags_key(target).removesuffix("_RUSTFLAGS") + "_LINKER"


# llvm-bolt refuses erratum 843419 veneers, so the BOLT link drops rustc's flag
# (lld has no negation) and stops gcc's spec re-adding it. The binary is unsafe
# on Cortex-A53, accepted for Graviton and Ampere-class targets; a Cortex-A53
# deployment needs PGO-only aarch64 builds.
_A53_FIX_LINK_ARG = "-Wl,--fix-cortex-a53-843419"
_NO_A53_FIX_DRIVER_ARG = "-mno-fix-cortex-a53-843419"

_BOLT_LINKER_DIR = CACHE_DIR / "bolt-linker"

_A53_STRIP_WRAPPER = """\
#!{python} -IS
import os
import sys

REAL = {real!r}
DROP = {drop!r}
ADD = {add!r}


def strip_response_file(arg):
    if not arg.startswith("@"):
        return arg
    path = arg[1:]
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            lines = f.read().split("\\n")
    except OSError:
        return arg
    if DROP not in lines:
        return arg
    stripped = path + ".no-a53-fix"
    with open(stripped, "w", encoding="utf-8", errors="surrogateescape", newline="\\n") as f:
        f.write("\\n".join(line for line in lines if line != DROP))
    return "@" + stripped


args = [strip_response_file(arg) for arg in sys.argv[1:] if arg != DROP]
os.execv(REAL, [REAL, *args, ADD])
"""


def _real_aarch64_linker(target: str, extra_env: dict[str, str] | None) -> str | None:
    """Return the absolute path of the linker driver the BOLT link would run.

    Order: CARGO_TARGET_<TRIPLE>_LINKER from ``extra_env``, then the process
    env, then ``<gnu-triple>-gcc``, then ``cc``. Symlinks are kept, because
    ccache and clang pick their mode from argv[0].
    """
    key = _target_linker_key(target)
    parts = target.split("-")
    gnu_triple = f"{parts[0]}-{parts[2]}-{parts[3]}" if len(parts) == 4 else target
    candidates = [
        ((extra_env or {}).get(key), f"{key} (build env)"),
        (os.environ.get(key), f"{key} (process env)"),
        (f"{gnu_triple}-gcc", "target gcc on PATH"),
        ("cc", "cc on PATH"),
    ]
    for name, source in candidates:
        if not name:
            continue
        resolved = shutil.which(name)
        if resolved:
            real = os.path.abspath(resolved)
            info(f"BOLT: real linker for {target} is {real} ({source})")
            return real
        warn(f"BOLT: {source} names {name}, which does not resolve -- skipping it")
    return None


def _a53_strip_linker(real_linker: str) -> Path:
    """Write the wrapper that links through ``real_linker`` without the A53 fix.

    Python rather than sh, because past ARG_MAX rustc passes an
    ``@linker-arguments`` file that needs rewriting line by line. The name
    carries a content digest, so concurrent jobs write identical bytes and the
    rename never exposes a half-written file.
    """
    script = _A53_STRIP_WRAPPER.format(
        python=sys.executable,
        real=real_linker,
        drop=_A53_FIX_LINK_ARG,
        add=_NO_A53_FIX_DRIVER_ARG,
    )
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:12]
    _BOLT_LINKER_DIR.mkdir(parents=True, exist_ok=True)
    wrapper = _BOLT_LINKER_DIR / f"no-a53-fix-{digest}"
    staging = wrapper.with_name(f"{wrapper.name}.{os.getpid()}")
    staging.write_text(script, encoding="utf-8", newline="\n")
    staging.chmod(
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )
    staging.replace(wrapper)
    return wrapper


def _a53_linker_env(target: str, extra_env: dict[str, str] | None) -> dict[str, str]:
    """Return CARGO_TARGET_<TRIPLE>_LINKER set to the A53 strip wrapper, or {}."""
    real = _real_aarch64_linker(target, extra_env)
    if real is None:
        warn(
            f"BOLT: no linker driver resolves for {target}, so the link keeps the "
            "Cortex-A53 erratum fix and llvm-bolt will refuse its veneers"
        )
        return {}
    wrapper = _a53_strip_linker(real)
    info(
        f"BOLT: linking {target} through {wrapper}, which drops "
        f"{_A53_FIX_LINK_ARG} and adds {_NO_A53_FIX_DRIVER_ARG}"
    )
    return {_target_linker_key(target): str(wrapper)}


def _bolt_build_env(
    target: str,
    *,
    no_split: bool = False,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return linker env overrides for the cargo-pgo BOLT build and optimize steps.

    The linker must be lld: `--emit-relocs` segfaults mold and GNU BFD rejects
    it. Only RUSTFLAGS and the linker change, as neither reaches cargo's
    symbol-name hash. Cargo joins the value onto the project's
    `target.<triple>.rustflags`, but a project with only `build.rustflags`
    loses them for these steps. On aarch64 the linker becomes the A53 strip
    wrapper.
    """
    target_rustflags_key = _target_rustflags_key(target)
    bolt_rustflags = "-C link-arg=-fuse-ld=lld"
    if no_split:
        extra = _bolt_no_split_rustflags()
        if extra:
            bolt_rustflags = f"{bolt_rustflags} {extra}"
    env = {target_rustflags_key: bolt_rustflags}
    if target.startswith("aarch64") and "linux" in target:
        env.update(_a53_linker_env(target, extra_env))
    return env


# cargo-pgo's default llvm-bolt optimise flags, copied from src/bolt/optimize.rs
# of the pinned cargo-pgo, for a HYPERCI_BOLT_OPTIMIZE_ARGS bisect to start from
# (--bolt-args replaces them). tests/unit/test_rust_pgo.py fails when the pin
# moves away from this version.
_CARGO_PGO_FLAGS_VERIFIED_AGAINST = "0.3.0"

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


# Replaces cargo-pgo's BOLT flags for one validate-only run, from the rust-ci.yml
# `bolt-optimize-args` dispatch input (issue #262); build.py refuses it on a
# run that ships.
BOLT_OPTIMIZE_ARGS_ENV = "HYPERCI_BOLT_OPTIMIZE_ARGS"

# One llvm-bolt option as a single token: dash-led, `=` for a value. cargo-pgo
# shell-splits `--bolt-args`, so quotes, spaces and metacharacters stay out.
_BOLT_ARG_TOKEN = re.compile(r"-{1,2}[A-Za-z0-9][A-Za-z0-9_.,:+=-]*")


def bolt_optimize_args_override() -> tuple[str, ...] | None:
    """Return the per-run BOLT optimise flags, or None when unset or blank.

    Raises:
        ValueError: A token is not one dash-led llvm-bolt option.

    """
    tokens = os.environ.get(BOLT_OPTIMIZE_ARGS_ENV, "").split()
    if not tokens:
        return None
    bad = [token for token in tokens if not _BOLT_ARG_TOKEN.fullmatch(token)]
    if bad:
        rejected = " ".join(repr(token) for token in bad)
        raise ValueError(
            f"bolt-optimize-args rejects {rejected}: each flag is one token that "
            "starts with '-' and holds only letters, digits and _ . , : + = -. "
            "Give a value as -name=value."
        )
    return tuple(tokens)


def _bolt_optimize_args() -> list[str]:
    """Return `--bolt-args` from `HYPERCI_BOLT_OPTIMIZE_ARGS`, else empty.

    Empty keeps cargo-pgo's own llvm-bolt flags on every architecture.
    """
    override = bolt_optimize_args_override()
    if override is None:
        return []
    return ["--bolt-args", " ".join(override)]


def _attempt_bolt(
    target: str,
    cargo_args: list[str],
    binary_name: str,
    profile: OptimizationProfile,
    cwd: Path,
    extra_env: dict[str, str] | None,
    *,
    no_split: bool,
    outcome: OptimizationOutcome | None = None,
) -> int:
    """Run one BOLT pass: instrument -> workload -> optimise.

    The workload must run against `<binary>-bolt-instrumented`, or `bolt
    optimize` has no profile (#29). Both builds use `--with-pgo` and skip
    sccache. ``no_split`` disables the compiler cold-splitter.

    Returns 0 on success or on a non-fatal skip that leaves the PGO-only
    binary; non-zero only when a BOLT build fails, which _run_bolt retries.
    """
    bolt_overrides = _bolt_build_env(target, no_split=no_split, extra_env=extra_env)
    bolt_env = {**(extra_env or {}), **bolt_overrides, **_PROFILE_USE_ENV}
    label = " (no-split)" if no_split else ""

    if _target_linker_key(target) in bolt_overrides:
        info(
            f"BOLT: {target} links without the Cortex-A53 erratum 843419 fix -- "
            "the shipped binary is not safe on Cortex-A53"
        )

    info(
        f"BOLT: building instrumented binary for {target} (linker forced to lld){label}"
    )
    rc = _run_cargo_pgo(
        [
            "bolt",
            "build",
            _BOLT_WITH_PGO,
            "--",
            "--target",
            target,
            *cargo_args,
        ],
        cwd=cwd,
        extra_env=bolt_env,
    )
    if rc != 0:
        return rc

    bolt_bin = _instrumented_binary_path(cwd, target, binary_name, variant="bolt")
    if not bolt_bin.exists():
        warn(
            f"BOLT-instrumented binary not found at {bolt_bin} -- "
            "skipping BOLT (PGO-only result stands)"
        )
        return 0
    rc = _run_workload(
        profile.pgo_workload_cmd or "",
        profile.pgo_duration_secs,
        bolt_bin,
        cwd=cwd,
    )
    if rc != 0:
        warn("BOLT workload failed -- skipping BOLT optimise (PGO-only result stands)")
        return 0

    info(
        f"BOLT: optimising binary for {target} (using PGO + BOLT profiles, linker=lld){label}"
    )
    optimize_args = _bolt_optimize_args()
    if optimize_args:
        warn(f"BOLT: optimise flags overridden for {target}: {optimize_args[1]}")
    rc = _run_cargo_pgo(
        [
            "bolt",
            "optimize",
            _BOLT_WITH_PGO,
            *optimize_args,
            "--",
            "--target",
            target,
            *cargo_args,
        ],
        cwd=cwd,
        extra_env=bolt_env,
    )
    # The skips above also return 0, so BOLT counts only once its file is installed.
    if rc == 0 and _install_bolt_output(cwd, target, binary_name) and outcome:
        outcome.bolt_applied = True
    return rc


def _run_bolt(
    target: str,
    cargo_args: list[str],
    binary_name: str,
    profile: OptimizationProfile,
    cwd: Path,
    extra_env: dict[str, str] | None,
    outcome: OptimizationOutcome | None = None,
) -> int:
    """Run BOLT, retrying once with compiler function-splitting disabled.

    Only a failed BOLT build retries, so apps that already optimise cleanly
    never see the no-split flags. Failing both leaves the PGO-only binary.
    """
    rc = _attempt_bolt(
        target,
        cargo_args,
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
        return rc
    warn(
        "BOLT build failed -- retrying once with compiler function-splitting "
        f"disabled ({no_split_flags})"
    )
    return _attempt_bolt(
        target,
        cargo_args,
        binary_name,
        profile,
        cwd,
        extra_env,
        no_split=True,
        outcome=outcome,
    )
