# Project:   HyperI CI
# File:      src/hyperi_ci/bootstrap.py
# Purpose:   Install language toolchains (Rust, Go, Node) for a runner image
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install the language toolchains a runner image pre-bakes.

Separate from `native_deps` because these come from vendor channels (rustup,
go.dev, nvm) rather than apt, and hyperi-ci does not install them per job (for
example `languages/rust/build.py` runs `rustup target add` with no bootstrap
behind it).

rustup-init, the Go tarball and nvm's install.sh are versions.yaml pins, each
checked against its pinned sha256 before it runs or is unpacked. Nothing here
asks an API which release is current.

The one cargo tool baked is sccache, because the ARC image sets
``RUSTC_WRAPPER=sccache`` and no CI step installs it. cargo-audit, cargo-deny
and cargo-nextest are not baked: the setup-rust-tools and setup-nextest
composites install their pinned builds on every job.

Every step is idempotent, so a partial failure can be re-run. Linux only, and
a no-op elsewhere.
"""

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from scalo import logger

from hyperi_ci.common import curl_fetch, sudo_prefix
from hyperi_ci.native_tools import _linux_arch, install_into
from hyperi_ci.versions import (
    runtime_sha256,
    runtime_version,
    tool_sha256,
    tool_version,
)

_CONFIG_FILE = Path(__file__).resolve().parent / "config" / "bootstrap.yaml"
_NVM_PROFILE = Path("/etc/profile.d/nvm.sh")
# The Go tarball unpacks to a `go/` directory under this.
_GO_PARENT = Path("/usr/local")

# Vendor install channels, deliberately not configurable. The version and the
# digest of what each serves come from versions.yaml.
_RUSTUP_INIT_URL = (
    "https://static.rust-lang.org/rustup/archive/{version}/{key}-unknown-linux-gnu/"
    "rustup-init"
)
_GO_DOWNLOAD_BASE = "https://go.dev/dl"
_NVM_INSTALL_BASE = "https://raw.githubusercontent.com/nvm-sh/nvm"
# `tools.rustup.sha256` is keyed by the target triple's arch.
_RUSTUP_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}
# The Go tarball is about 70 MB, so a 300-second attempt still finishes at 250 KB/s.
_DOWNLOAD_MAX_TIME = 300


@dataclass
class RustSpec:
    """What to install for Rust."""

    channels: list[str] = field(default_factory=lambda: ["stable"])
    components: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _run(cmd: list[str], env: Mapping[str, str] | None = None) -> int:
    """Run a command, streaming output. Returns the exit code."""
    logger.info(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, env=env, check=False).returncode


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _download_verified(
    name: str,
    url: str,
    dest: Path,
    expected: str,
    *,
    extra: tuple[str, ...] = (),
    follow_redirects: bool = True,
) -> int:
    """Download ``url`` to ``dest`` and check it against its pinned sha256.

    Args:
        name: What is being fetched, for the log lines.
        url: Where from.
        dest: File the body is written to.
        expected: The sha256 versions.yaml pins for it.
        extra: More curl options, placed before the URL.
        follow_redirects: Pass ``-L``.

    Returns:
        0 when ``dest`` matches the pin. curl's exit code after a failed
        download, or 1 after a digest mismatch, which also deletes ``dest``.

    """
    logger.info(f"  Downloading {url}")
    rc = curl_fetch(
        url,
        dest,
        extra=extra,
        follow_redirects=follow_redirects,
        max_time=_DOWNLOAD_MAX_TIME,
    ).returncode
    if rc != 0:
        logger.error(f"Failed to download {name} (curl exit {rc})")
        return rc
    with dest.open("rb") as fh:
        got = hashlib.file_digest(fh, "sha256").hexdigest()
    want = expected.strip().lower()
    if got != want:
        logger.error(
            f"{name}: SHA256 mismatch - refusing to install "
            f"(expected {want}, got {got})"
        )
        dest.unlink(missing_ok=True)
        return 1
    return 0


def load_spec() -> tuple[RustSpec, bool]:
    """Read bootstrap.yaml into (rust, go_enabled)."""
    with _CONFIG_FILE.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    rust_raw = raw.get("rust", {}) or {}
    go_enabled = bool((raw.get("go", {}) or {}).get("enabled", False))

    rust = RustSpec(
        channels=[str(c) for c in rust_raw.get("channels", ["stable"])],
        components=[str(c) for c in rust_raw.get("components", [])],
        targets=[str(t) for t in rust_raw.get("targets", [])],
    )
    return rust, go_enabled


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------


def install_sccache(bin_dir: Path) -> int:
    """Install the versions.yaml sccache into ``bin_dir``, replacing any copy there.

    Not the hyperi-ci cache, because every job finds sccache as
    ``RUSTC_WRAPPER`` on the image's PATH.

    Args:
        bin_dir: Directory on the image's PATH, normally ``$CARGO_HOME/bin``.

    Returns:
        0 on success, 1 on an unknown CPU, a failed download, a digest
        mismatch or a tarball without the binary.

    """
    logger.info(f"Installing sccache {tool_version('sccache')}")
    return 0 if install_into("sccache", bin_dir) else 1


def install_rustup(default_channel: str) -> int:
    """Run the versions.yaml rustup-init, checked against its pinned sha256.

    Args:
        default_channel: The toolchain rustup-init installs as the default.

    Returns:
        0 on success, else non-zero after logging: an unknown CPU, a failed
        download, a digest mismatch, or rustup-init failing.

    """
    arch = _linux_arch()
    if arch is None:
        logger.error(f"No pinned rustup-init for {platform.machine()}")
        return 1
    key = _RUSTUP_ARCH[arch]
    version = tool_version("rustup")
    logger.info(f"Installing rustup {version}")
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-rustup-") as scratch:
        init = Path(scratch) / "rustup-init"
        # rustup's own transport rules: https only, TLS 1.2 or later, no redirects.
        rc = _download_verified(
            "rustup-init",
            _RUSTUP_INIT_URL.format(version=version, key=key),
            init,
            tool_sha256("rustup", key),
            extra=("--proto", "=https", "--tlsv1.2"),
            follow_redirects=False,
        )
        if rc != 0:
            return rc
        init.chmod(0o755)
        rc = _run([str(init), "-y", "--default-toolchain", default_channel])
    if rc != 0:
        logger.error("rustup install failed")
    return rc


def install_rust(spec: RustSpec) -> int:
    """Install rustup, the requested channels, components and targets, and sccache.

    Honours RUSTUP_HOME / CARGO_HOME if set (an image puts them on a shared
    path).
    """
    if not _is_linux():
        logger.info("Skipping Rust bootstrap on non-Linux")
        return 0

    default_channel = spec.channels[0] if spec.channels else "stable"

    if _have("rustup"):
        logger.info("rustup already installed")
    else:
        rc = install_rustup(default_channel)
        if rc != 0:
            return rc

    # CARGO_HOME/bin is not on this process's PATH yet.
    cargo_home = Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")))
    cargo_bin = cargo_home / "bin"
    if str(cargo_bin) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = f"{cargo_bin}{os.pathsep}{os.environ.get('PATH', '')}"

    for channel in spec.channels:
        rc = _run(["rustup", "toolchain", "install", channel])
        if rc != 0:
            logger.error(f"rustup toolchain install {channel} failed")
            return rc

    if spec.components:
        rc = _run(["rustup", "component", "add", *spec.components])
        if rc != 0:
            logger.error(f"rustup component add failed: {spec.components}")
            return rc

    for target in spec.targets:
        rc = _run(["rustup", "target", "add", target])
        if rc != 0:
            logger.error(f"rustup target add {target} failed")
            return rc

    return install_sccache(cargo_bin)


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def install_go() -> int:
    """Install the versions.yaml ``runtimes.go`` into /usr/local/go.

    The tarball is checked against its pinned sha256 before it is unpacked.
    """
    if not _is_linux():
        logger.info("Skipping Go bootstrap on non-Linux")
        return 0

    if (_GO_PARENT / "go" / "bin" / "go").exists():
        logger.info(f"Go already installed at {_GO_PARENT / 'go'}")
        return 0

    arch = _linux_arch()
    if arch is None:
        logger.error(f"No pinned Go build for {platform.machine()}")
        return 1
    version = runtime_version("go")
    logger.info(f"Installing Go {version}")
    tarball = f"go{version}.linux-{arch}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="hyperi-ci-go-") as scratch:
        dest = Path(scratch) / tarball
        rc = _download_verified(
            tarball,
            f"{_GO_DOWNLOAD_BASE}/{tarball}",
            dest,
            runtime_sha256("go", arch),
        )
        if rc != 0:
            return rc

        rc = _run([*sudo_prefix(), "tar", "-C", str(_GO_PARENT), "-xzf", str(dest)])
    if rc != 0:
        logger.error("Failed to extract the Go tarball")
        return rc

    return 0


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def install_nvm(nvm_dir: Path) -> int:
    """Install the versions.yaml nvm tag into ``nvm_dir``.

    Args:
        nvm_dir: The ``NVM_DIR`` install.sh writes nvm into.

    Returns:
        0 on success, else non-zero after logging: a failed download, a digest
        mismatch, or install.sh failing.

    """
    tag = tool_version("nvm")
    logger.info(f"Installing nvm {tag} into {nvm_dir}")
    nvm_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-nvm-") as scratch:
        script = Path(scratch) / "install.sh"
        rc = _download_verified(
            "nvm install.sh",
            f"{_NVM_INSTALL_BASE}/{tag}/install.sh",
            script,
            tool_sha256("nvm", "script"),
        )
        if rc != 0:
            return rc
        rc = _run(["bash", str(script)], env={**os.environ, "NVM_DIR": str(nvm_dir)})
    if rc != 0:
        logger.error("nvm install failed")
    return rc


def install_node() -> int:
    """Install nvm and the versions.yaml ``runtimes.node`` major as the default.

    nvm is the versions.yaml ``tools.nvm`` tag, through that tag's install.sh
    checked against its pinned sha256. One Node major only, the one the CI
    workflows default to. ``--latest-npm`` because `npm install -g npm@latest`
    over an existing npm leaves a broken dependency tree (MODULE_NOT_FOUND:
    promise-retry), while nvm upgrades atomically.
    """
    if not _is_linux():
        logger.info("Skipping Node bootstrap on non-Linux")
        return 0

    nvm_dir = Path(os.environ.get("NVM_DIR", "/usr/local/nvm"))
    major = runtime_version("node")

    if not (nvm_dir / "nvm.sh").exists():
        rc = install_nvm(nvm_dir)
        if rc != 0:
            return rc

    # nvm is a shell function, so every call has to source it.
    script = (
        f'export NVM_DIR="{nvm_dir}"\n'
        f'. "$NVM_DIR/nvm.sh"\n'
        f'nvm install "{major}" --latest-npm && '
        f'nvm alias default "{major}" && '
        f"nvm cache clear\n"
        # Symlinks let a job that never sources nvm find node/npm/npx/corepack.
        f'DEFAULT_BIN="$NVM_DIR/versions/node/$(nvm version default)/bin"\n'
        f'for b in node npm npx corepack; do ln -sf "$DEFAULT_BIN/$b" '
        f'"/usr/local/bin/$b"; done\n'
    )
    logger.info(f"Installing Node {major}")
    rc = _run(["bash", "-c", script])
    if rc != 0:
        logger.error("Node install failed")
        return rc

    # Gives interactive shells on the runner nvm too.
    profile = _NVM_PROFILE
    try:
        profile.write_text(
            f'export NVM_DIR="{nvm_dir}"\n'
            '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"\n'
            '[ -s "$NVM_DIR/bash_completion" ] && . "$NVM_DIR/bash_completion"\n',
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        logger.warning(f"Could not write {profile}: {exc}")

    if _have("corepack"):
        _run(["corepack", "enable", "pnpm"])

    return 0


def install_python() -> int:
    """Install the versions.yaml ``runtimes.python`` CPython through uv.

    Bakes the interpreter every job's ``uvx hyperi-ci`` resolves to. It lands in
    ``UV_PYTHON_INSTALL_DIR``, which the image sets.
    """
    if not _is_linux():
        logger.info("Skipping Python bootstrap on non-Linux")
        return 0

    if not _have("uv"):
        logger.error("uv is required to install Python and was not found on PATH")
        return 1

    version = runtime_version("python")
    install_dir = os.environ.get("UV_PYTHON_INSTALL_DIR") or "uv default"
    logger.info(f"Installing Python {version} into {install_dir}")
    rc = _run(["uv", "python", "install", version])
    if rc != 0:
        logger.error("Python install failed")
    return rc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def install_toolchain_bootstrap() -> int:
    """Install Rust, Go, Node and Python for the runner image. Returns exit code."""
    if not _is_linux():
        logger.info(f"Skipping toolchain bootstrap on {platform.system()}")
        return 0

    rust, go_enabled = load_spec()

    logger.info("=== toolchain bootstrap: Rust ===")
    rc = install_rust(rust)
    if rc != 0:
        return rc

    if go_enabled:
        logger.info("=== toolchain bootstrap: Go ===")
        rc = install_go()
        if rc != 0:
            return rc

    logger.info("=== toolchain bootstrap: Node ===")
    rc = install_node()
    if rc != 0:
        return rc

    logger.info("=== toolchain bootstrap: Python ===")
    rc = install_python()
    if rc != 0:
        return rc

    logger.info("Toolchain bootstrap complete")
    return 0


def print_bootstrap_plan() -> None:
    """Print what the bootstrap would install (dry-run helper).

    Prints to stderr like `native_deps.print_needed`, so the two interleave.
    """
    rust, go_enabled = load_spec()
    out = sys.stderr
    print("  rust:", file=out)
    print(f"    rustup:      {tool_version('rustup')}", file=out)
    print(f"    channels:    {', '.join(rust.channels) or '-'}", file=out)
    print(f"    components:  {', '.join(rust.components) or '-'}", file=out)
    print(f"    targets:     {', '.join(rust.targets) or '-'}", file=out)
    print(f"    sccache:     {tool_version('sccache')}", file=out)
    print(f"  go: {runtime_version('go') if go_enabled else 'disabled'}", file=out)
    print(f"  node: {runtime_version('node')}", file=out)
    print(f"  nvm: {tool_version('nvm')}", file=out)
    print(f"  python: {runtime_version('python')}", file=out)
