# Project:   HyperI CI
# File:      src/hyperi_ci/native_tools.py
# Purpose:   Pinned native tools a project's tests opt into
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Pinned native tools for a project's tests and for ``lint-iac``.

The test stage installs every tool listed in ``test.native_tools`` before the
language handler runs, so a test that shells out to one finds the pinned build
on PATH. The runner images carry none of them, and the opt-in keeps every other
repo's Test job from paying for the download. ``lint-iac`` fetches helm, tofu
and kustomize through :func:`ci_binary` on the same terms.

Each tool comes from its upstream release, is checked against the digest in
versions.yaml, and is unpacked into the hyperi-ci cache, one directory per
version and arch. Nothing is written outside that cache and nothing needs sudo,
so it works on an ARC pod as well as a hosted runner. The directory goes on the
front of this process's PATH, which every test command inherits, so the pinned
build wins over any copy the runner image already has.

Linux only. Elsewhere a copy already on PATH is used with a warning that it is
not the pinned build, and a missing one fails the stage with install advice.
"""

import io
import os
import platform
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.quality.install import fetch_verified
from hyperi_ci.tools import missing_tool_notice
from hyperi_ci.upgrade import CACHE_DIR
from hyperi_ci.versions import tool_sha256, tool_version

CONFIG_KEY = "test.native_tools"


@dataclass(frozen=True, slots=True)
class NativeTool:
    """One tool's Linux release asset and how to confirm the install runs.

    ``url`` and ``member`` are format strings over ``{version}`` (verbatim from
    versions.yaml), ``{bare}`` (the version without its leading ``v``) and
    ``{arch}`` (the asset's own spelling, which is also the key of its digest
    in versions.yaml).
    """

    url: str
    member: str
    probe: tuple[str, ...]


_TOOLS: dict[str, NativeTool] = {
    "helm": NativeTool(
        url="https://get.helm.sh/helm-{version}-linux-{arch}.tar.gz",
        member="linux-{arch}/helm",
        probe=("version", "--short"),
    ),
    "kustomize": NativeTool(
        url=(
            "https://github.com/kubernetes-sigs/kustomize/releases/download/"
            "kustomize%2F{version}/kustomize_{version}_linux_{arch}.tar.gz"
        ),
        member="kustomize",
        probe=("version",),
    ),
    "tofu": NativeTool(
        url=(
            "https://github.com/opentofu/opentofu/releases/download/"
            "{version}/tofu_{bare}_linux_{arch}.tar.gz"
        ),
        member="tofu",
        probe=("version",),
    ),
}

# platform.machine() -> the arch as the assets above spell it.
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class NativeToolError(ValueError):
    """``test.native_tools`` is malformed or names a tool hyperi-ci cannot install."""


def known_tools() -> list[str]:
    """Every tool name ``test.native_tools`` accepts, sorted."""
    return sorted(_TOOLS)


def requested_tools(config: CIConfig) -> list[str]:
    """Return the tools the project listed, in order, without duplicates.

    Raises:
        NativeToolError: The value is not a list of names, or a name is not one
            hyperi-ci knows how to install. An unknown name is refused rather
            than skipped: the project asked for it because its tests need it.

    """
    raw = config.get(CONFIG_KEY)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        raise NativeToolError(
            f"{CONFIG_KEY} must be a list of tool names, such as [helm]; got {raw!r}"
        )
    names = list(dict.fromkeys(raw))
    unknown = [n for n in names if n not in _TOOLS]
    if unknown:
        raise NativeToolError(
            f"{CONFIG_KEY} names a tool hyperi-ci cannot install: "
            f"{', '.join(repr(n) for n in unknown)}. "
            f"Known: {', '.join(known_tools())}"
        )
    return names


def _linux_arch() -> str | None:
    """The asset arch for this host, or None off Linux or on an unknown CPU."""
    if sys.platform != "linux":
        return None
    return _ARCH.get(platform.machine().lower())


def _unpinned_on_path(name: str) -> Path | None:
    """Fall back to a copy already on PATH where no pinned build exists."""
    exe = shutil.which(name)
    if exe is None:
        error(
            missing_tool_notice(
                name,
                purpose=f"the tests this project opted into through {CONFIG_KEY}",
            )
        )
        return None
    warn(
        f"  {name}: using {exe}, not the pinned {tool_version(name)} - "
        f"hyperi-ci installs the pinned build on Linux x86_64 and aarch64 only"
    )
    return Path(exe).parent


def install_tool(name: str, cache_dir: Path = CACHE_DIR) -> Path | None:
    """Return the directory holding the pinned ``name``, installing it on first use.

    Args:
        name: A key of the tool registry.
        cache_dir: The hyperi-ci cache root.

    Returns:
        The directory to put on PATH, or None after logging why there is none.

    """
    arch = _linux_arch()
    if arch is None:
        return _unpinned_on_path(name)

    tool = _TOOLS[name]
    version = tool_version(name)
    bin_dir = cache_dir / "native-tools" / name / f"{version}-{arch}"
    binary = bin_dir / name
    if binary.is_file():
        return bin_dir

    bare = version.removeprefix("v")
    url = tool.url.format(version=version, bare=bare, arch=arch)
    payload = fetch_verified(name, url, tool_sha256(name, arch))
    if payload is None:
        return None

    member = tool.member.format(version=version, bare=bare, arch=arch)
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            extracted = archive.extractfile(member)
            data = extracted.read() if extracted else b""
    except (tarfile.TarError, KeyError, OSError):
        data = b""
    if not data:
        error(f"  {name}: '{member}' not found in {url}")
        return None

    # Written under a temporary name and renamed, so an interrupted install
    # never leaves a truncated binary that the is_file() check above would reuse.
    bin_dir.mkdir(parents=True, exist_ok=True)
    partial = bin_dir / f".{name}.partial"
    partial.write_bytes(data)
    partial.chmod(0o755)
    partial.replace(binary)
    return bin_dir


def ci_binary(name: str) -> str | None:
    """Return ``name`` from PATH, else the pinned build installed on Linux CI.

    The quality-gate rule for a missing tool: a local run uses what the
    developer has and the caller warn-skips when it is absent, while CI gets
    the pinned build. Returns None off CI or off Linux when nothing is on PATH.
    """
    exe = shutil.which(name)
    if exe:
        return exe
    if not is_ci() or _linux_arch() is None:
        return None
    bin_dir = install_tool(name)
    return str(bin_dir / name) if bin_dir else None


def prepare(config: CIConfig) -> int:
    """Install every tool in ``test.native_tools`` and put it on PATH.

    Returns:
        0 when every listed tool runs (or none is listed), 1 otherwise.

    """
    try:
        names = requested_tools(config)
    except NativeToolError as exc:
        error(str(exc))
        return 1
    if not names:
        return 0

    bin_dirs: list[str] = []
    for name in names:
        info(f"Installing {name} {tool_version(name)} for the tests ({CONFIG_KEY})")
        bin_dir = install_tool(name)
        if bin_dir is None:
            error(f"{name} is not available, so the tests that need it cannot run")
            return 1
        bin_dirs.append(str(bin_dir))
    os.environ["PATH"] = os.pathsep.join([*bin_dirs, os.environ.get("PATH", "")])

    for name in names:
        exe = shutil.which(name)
        if exe is None:
            error(f"  {name}: installed but not found on PATH")
            return 1
        try:
            probe = run_cmd([exe, *_TOOLS[name].probe], check=False, capture=True)
        except OSError as exc:
            error(f"  {name}: {exe} does not run: {exc}")
            return 1
        if probe.returncode != 0:
            error(f"  {name}: {exe} does not run: {probe.stderr.strip()}")
            return 1
        info(f"  {name}: {exe} ({probe.stdout.strip()})")
    return 0
