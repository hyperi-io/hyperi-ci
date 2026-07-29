# Project:   HyperI CI
# File:      src/hyperi_ci/bootstrap.py
# Purpose:   Install language toolchains (Rust, Go, Node) for a runner image
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install the language toolchains a runner image pre-bakes.

Separate from `native_deps` because these come from vendor channels (rustup,
go.dev, nvm) rather than apt, and because hyperi-ci does NOT install them per
job -- it assumes they exist. `languages/rust/build.py` calls `rustup target
add` with no bootstrap behind it.

That assumption is exactly why baking them pays: the cargo tools are source
builds costing tens of minutes cold, and nothing reinstalls them over the top.
The linters are the opposite case and are deliberately not here.

Every step is idempotent -- an already-present toolchain is left alone, so a
rebuild over a warm cache is cheap and a partial failure can be re-run.

Linux only. No-ops elsewhere, so importing this on a macOS workstation is
safe but does nothing useful.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from scalo import logger

_CONFIG_FILE = Path(__file__).resolve().parent / "config" / "bootstrap.yaml"

# Vendor install channels. Not configurable: changing where Rust comes from is
# not a knob, it is a different decision entirely.
_RUSTUP_URL = "https://sh.rustup.rs"
_GO_VERSION_URL = "https://go.dev/VERSION?m=text"
_GO_DOWNLOAD_BASE = "https://go.dev/dl"
_NVM_INSTALL_BASE = "https://raw.githubusercontent.com/nvm-sh/nvm"
_CARGO_BINSTALL_URL = "https://raw.githubusercontent.com/cargo-bins/cargo-binstall/main/install-from-binstall-release.sh"


@dataclass
class RustSpec:
    """What to install for Rust."""

    channels: list[str] = field(default_factory=lambda: ["stable"])
    components: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    cargo_tools: list[str] = field(default_factory=list)


@dataclass
class NodeSpec:
    """Node majors to preload via nvm, and which is default on PATH."""

    versions: list[str] = field(default_factory=list)
    default: str = ""


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _sudo_prefix() -> list[str]:
    """`sudo` when non-root, nothing when already root.

    A Dockerfile RUN executes as root with no sudo configured, which would
    otherwise fail with 'root is not in the sudoers file'.
    """
    if not _is_linux():
        return []
    return [] if os.geteuid() == 0 else ["sudo"]


def _run(cmd: list[str], *, shell_input: str | None = None) -> int:
    """Run a command, streaming output. Returns the exit code."""
    logger.info(f"  $ {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        input=shell_input,
        text=True if shell_input is not None else False,
        encoding="utf-8" if shell_input is not None else None,
        errors="replace" if shell_input is not None else None,
        check=False,
    )
    return result.returncode


def _capture(cmd: list[str]) -> tuple[int, str]:
    """Run a command and capture stdout. Returns (exit code, stdout)."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.returncode, result.stdout.strip()


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def load_spec() -> tuple[RustSpec, bool, NodeSpec]:
    """Read bootstrap.yaml into (rust, go_enabled, node)."""
    with _CONFIG_FILE.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    rust_raw = raw.get("rust", {}) or {}
    node_raw = raw.get("node", {}) or {}
    go_enabled = bool((raw.get("go", {}) or {}).get("enabled", False))

    rust = RustSpec(
        channels=[str(c) for c in rust_raw.get("channels", ["stable"])],
        components=[str(c) for c in rust_raw.get("components", [])],
        targets=[str(t) for t in rust_raw.get("targets", [])],
        cargo_tools=[str(t) for t in rust_raw.get("cargo_tools", [])],
    )
    node = NodeSpec(
        versions=[str(v) for v in node_raw.get("versions", [])],
        default=str(node_raw.get("default", "")),
    )
    return rust, go_enabled, node


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------


def _install_cargo_tools(tools: list[str]) -> int:
    """Install cargo binaries, preferring prebuilt releases.

    `cargo install` compiles from source: sccache, cargo-deny, cargo-audit and
    cargo-nextest together are tens of minutes. All four publish prebuilt
    binaries, so cargo-binstall fetches those instead and the image build drops
    to minutes. Falls back to a source build per tool if binstall is
    unavailable or a fetch fails -- slow, but the image is still correct.
    """
    if not tools:
        return 0

    if not _have("cargo-binstall"):
        logger.info("Installing cargo-binstall (prebuilt cargo tool fetcher)")
        # The installer script is fetched and piped to a shell. Upstream ships
        # no signed artefact for it; the alternative is a source build of
        # binstall itself, which defeats the point.
        dl = subprocess.run(
            ["curl", "-fsSL", _CARGO_BINSTALL_URL],
            capture_output=True,
            check=False,
        )
        if dl.returncode == 0 and dl.stdout:
            rc = subprocess.run(["bash"], input=dl.stdout, check=False).returncode
            if rc != 0:
                logger.warning(
                    "cargo-binstall install failed - falling back to source builds"
                )
        else:
            logger.warning(
                "cargo-binstall download failed - falling back to source builds"
            )

    failed: list[str] = []
    for tool in tools:
        binary = tool
        if _have(binary):
            logger.info(f"[{tool}] already installed")
            continue

        installed = False
        if _have("cargo-binstall"):
            logger.info(f"Installing {tool} (prebuilt)")
            installed = _run(["cargo", "binstall", "--no-confirm", tool]) == 0
            if not installed:
                logger.warning(f"[{tool}] binstall failed - building from source")

        if not installed:
            logger.info(f"Installing {tool} (source build)")
            installed = _run(["cargo", "install", tool, "--locked"]) == 0

        if not installed:
            failed.append(tool)

    if failed:
        logger.error(f"cargo tools failed to install: {failed}")
        return 1
    return 0


def install_rust(spec: RustSpec) -> int:
    """Install rustup, the requested channels, components, targets and tools.

    Honours RUSTUP_HOME / CARGO_HOME if the caller set them (a runner image
    puts them on a shared path so every job sees the same toolchain).
    """
    if not _is_linux():
        logger.info("Skipping Rust bootstrap on non-Linux")
        return 0

    default_channel = spec.channels[0] if spec.channels else "stable"

    if _have("rustup"):
        logger.info("rustup already installed")
    else:
        logger.info("Installing rustup")
        dl = subprocess.run(
            ["curl", "--proto", "=https", "--tlsv1.2", "-sSf", _RUSTUP_URL],
            capture_output=True,
            check=False,
        )
        if dl.returncode != 0 or not dl.stdout:
            logger.error("Failed to download rustup installer")
            return dl.returncode or 1
        rc = subprocess.run(
            ["sh", "-s", "--", "-y", "--default-toolchain", default_channel],
            input=dl.stdout,
            check=False,
        ).returncode
        if rc != 0:
            logger.error("rustup install failed")
            return rc

    # rustup drops binaries in CARGO_HOME/bin, which is not on PATH yet in the
    # same process. Add it so the calls below resolve.
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

    return _install_cargo_tools(spec.cargo_tools)


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def install_go() -> int:
    """Install the current Go stable release into /usr/local/go.

    go.dev publishes the current stable version as plain text, so there is no
    version to pin here -- the image tracks stable and is rolled back by
    re-tagging rather than by pinning.
    """
    if not _is_linux():
        logger.info("Skipping Go bootstrap on non-Linux")
        return 0

    if Path("/usr/local/go/bin/go").exists():
        logger.info("Go already installed at /usr/local/go")
        return 0

    rc, version = _capture(["curl", "-fsSL", _GO_VERSION_URL])
    if rc != 0 or not version:
        logger.error("Failed to resolve the current Go version")
        return rc or 1
    # The endpoint returns e.g. "go1.26.0\ntime ..." -- first line, minus "go"
    version = version.splitlines()[0].strip().removeprefix("go")
    logger.info(f"Installing Go {version}")

    arch = "arm64" if platform.machine() in ("aarch64", "arm64") else "amd64"
    tarball = f"go{version}.linux-{arch}.tar.gz"
    dest = Path("/tmp") / tarball  # noqa: S108 - transient download, removed below

    rc = _run(["curl", "-fsSL", "-o", str(dest), f"{_GO_DOWNLOAD_BASE}/{tarball}"])
    if rc != 0:
        logger.error(f"Failed to download {tarball}")
        return rc

    rc = _run([*_sudo_prefix(), "tar", "-C", "/usr/local", "-xzf", str(dest)])
    dest.unlink(missing_ok=True)
    if rc != 0:
        logger.error("Failed to extract the Go tarball")
        return rc

    return 0


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def install_node(spec: NodeSpec) -> int:
    """Install nvm and preload every requested Node major.

    Why nvm rather than NodeSource: NodeSource ships one Node per system.
    Carrying every active LTS lets repos pin via .nvmrc or engines.node
    without `actions/setup-node` re-downloading Node on every job.

    Why the majors are installed with --latest-npm: `npm install -g npm@latest`
    over an existing npm leaves a broken dependency tree
    (MODULE_NOT_FOUND: promise-retry). nvm does the upgrade atomically during
    install instead.
    """
    if not _is_linux():
        logger.info("Skipping Node bootstrap on non-Linux")
        return 0
    if not spec.versions:
        logger.info("No Node versions requested")
        return 0

    nvm_dir = Path(os.environ.get("NVM_DIR", "/usr/local/nvm"))
    default = spec.default or spec.versions[-1]

    if not (nvm_dir / "nvm.sh").exists():
        rc, tag = _capture(
            [
                "curl",
                "-fsSL",
                "https://api.github.com/repos/nvm-sh/nvm/releases/latest",
            ]
        )
        if rc != 0 or not tag:
            logger.error("Failed to resolve the current nvm release")
            return rc or 1
        import json

        try:
            nvm_tag = json.loads(tag)["tag_name"]
        except (ValueError, KeyError):
            logger.error("Could not parse the nvm release response")
            return 1

        logger.info(f"Installing nvm {nvm_tag} into {nvm_dir}")
        nvm_dir.mkdir(parents=True, exist_ok=True)
        dl = subprocess.run(
            ["curl", "-fsSL", f"{_NVM_INSTALL_BASE}/{nvm_tag}/install.sh"],
            capture_output=True,
            check=False,
        )
        if dl.returncode != 0 or not dl.stdout:
            logger.error("Failed to download the nvm installer")
            return dl.returncode or 1
        env = {**os.environ, "NVM_DIR": str(nvm_dir)}
        rc = subprocess.run(["bash"], input=dl.stdout, env=env, check=False).returncode
        if rc != 0:
            logger.error("nvm install failed")
            return rc

    # nvm is a shell function, not a binary -- every call has to source it.
    installs = " && ".join(f'nvm install "{v}" --latest-npm' for v in spec.versions)
    script = (
        f'export NVM_DIR="{nvm_dir}"\n'
        f'. "$NVM_DIR/nvm.sh"\n'
        f"{installs} && "
        f'nvm alias default "{default}" && '
        f"nvm cache clear\n"
        # Symlink the default major onto PATH so a job that never sources nvm
        # still finds node/npm/npx/corepack.
        f'DEFAULT_BIN="$NVM_DIR/versions/node/$(nvm version default)/bin"\n'
        f'for b in node npm npx corepack; do ln -sf "$DEFAULT_BIN/$b" '
        f'"/usr/local/bin/$b"; done\n'
    )
    logger.info(f"Installing Node majors: {spec.versions} (default {default})")
    rc = subprocess.run(["bash", "-c", script], check=False).returncode
    if rc != 0:
        logger.error("Node install failed")
        return rc

    # Written so an interactive shell on the runner also gets nvm.
    profile = Path("/etc/profile.d/nvm.sh")
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def install_toolchain_bootstrap() -> int:
    """Install every language toolchain in bootstrap.yaml. Returns exit code."""
    if not _is_linux():
        logger.info(f"Skipping toolchain bootstrap on {platform.system()}")
        return 0

    rust, go_enabled, node = load_spec()

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
    rc = install_node(node)
    if rc != 0:
        return rc

    logger.info("Toolchain bootstrap complete")
    return 0


def print_bootstrap_plan() -> None:
    """Print what the bootstrap would install (dry-run helper).

    stderr, matching `native_deps.print_needed`, so the two interleave in
    install order rather than splitting across streams.
    """
    rust, go_enabled, node = load_spec()
    out = sys.stderr
    print("  rust:", file=out)
    print(f"    channels:    {', '.join(rust.channels) or '-'}", file=out)
    print(f"    components:  {', '.join(rust.components) or '-'}", file=out)
    print(f"    targets:     {', '.join(rust.targets) or '-'}", file=out)
    print(f"    cargo tools: {', '.join(rust.cargo_tools) or '-'}", file=out)
    print(f"  go: {'current stable' if go_enabled else 'disabled'}", file=out)
    print(
        f"  node: {', '.join(node.versions) or '-'} (default {node.default or '-'})",
        file=out,
    )
