# Project:   HyperI CI
# File:      src/hyperi_ci/native_tools.py
# Purpose:   Pinned release binaries: the test opt-in, the quality gates, the bake
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Pinned release binaries for a project's tests, the quality gates and the bake.

Every release binary hyperi-ci downloads comes through the table below. The
test stage installs every tool listed in ``test.native_tools`` before the
language handler runs, so a test that shells out to one finds the pinned build
on PATH. The quality gates and ``lint-iac`` fetch theirs through
:func:`ci_binary` when the runner has none, and the runner-image bake writes
sccache through :func:`install_into`.

Each tool comes from its upstream release and is checked against the digest in
versions.yaml before anything is unpacked. Apart from the baked sccache, it is
written into the hyperi-ci cache, one directory per version and arch. Nothing
needs sudo, so it works on an ARC pod as well as a hosted runner. The directory
goes on the front of this process's PATH, which every child command inherits.
For the tests that means the pinned build wins over any copy the runner image
already has. A quality gate uses an existing copy and installs only without one.

Linux only. Elsewhere a copy already on PATH is used with a warning that it is
not the pinned build, and a missing one fails the stage with install advice.
"""

import contextlib
import hashlib
import io
import os
import platform
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import download_artefact, error, info, is_ci, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.tools import missing_tool_notice
from hyperi_ci.upgrade import CACHE_DIR
from hyperi_ci.versions import tool_sha256, tool_version

CONFIG_KEY = "test.native_tools"


@dataclass(frozen=True, slots=True)
class NativeTool:
    """One tool's Linux release asset.

    ``url`` is a format string over ``{version}`` (verbatim from versions.yaml),
    ``{bare}`` (the version without its leading ``v``), ``{arch}`` and
    ``{asset}``. ``arch`` maps ``amd64`` / ``arm64`` to the spelling of the
    tool's digest keys in versions.yaml, which is also how most assets spell
    it. ``asset`` overrides that spelling for the URL alone. A ``.tar.gz`` is
    unpacked and the file named after the tool taken from it; any other asset
    is the binary itself.

    ``probe`` confirms the installed binary runs. Only a tool with one is
    offered to ``test.native_tools``.
    """

    url: str
    arch: dict[str, str] | None = None
    asset: dict[str, str] | None = None
    probe: tuple[str, ...] = ()


_MUSL_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}

_TOOLS: dict[str, NativeTool] = {
    "alint": NativeTool(
        url=(
            "https://github.com/asamarts/alint/releases/download/"
            "{version}/alint-{version}-{arch}-unknown-linux-musl.tar.gz"
        ),
        arch=_MUSL_ARCH,
    ),
    "gitleaks": NativeTool(
        url=(
            "https://github.com/gitleaks/gitleaks/releases/download/"
            "{version}/gitleaks_{bare}_linux_{arch}.tar.gz"
        ),
        arch={"amd64": "x64"},
    ),
    "hadolint": NativeTool(
        url=(
            "https://github.com/hadolint/hadolint/releases/download/"
            "{version}/hadolint-linux-{arch}"
        ),
        arch={"amd64": "x86_64"},
    ),
    "helm": NativeTool(
        url="https://get.helm.sh/helm-{version}-linux-{arch}.tar.gz",
        probe=("version", "--short"),
    ),
    "kube-linter": NativeTool(
        url=(
            "https://github.com/stackrox/kube-linter/releases/download/"
            "{version}/kube-linter-linux{asset}"
        ),
        asset={"amd64": "", "arm64": "_arm64"},
    ),
    "kubeconform": NativeTool(
        url=(
            "https://github.com/yannh/kubeconform/releases/download/"
            "{version}/kubeconform-linux-{arch}.tar.gz"
        ),
    ),
    "kustomize": NativeTool(
        url=(
            "https://github.com/kubernetes-sigs/kustomize/releases/download/"
            "kustomize%2F{version}/kustomize_{version}_linux_{arch}.tar.gz"
        ),
        probe=("version",),
    ),
    "lychee": NativeTool(
        url=(
            "https://github.com/lycheeverse/lychee/releases/download/"
            "lychee-v{version}/lychee-{asset}-unknown-linux-musl.tar.gz"
        ),
        asset=_MUSL_ARCH,
    ),
    "sccache": NativeTool(
        url=(
            "https://github.com/mozilla/sccache/releases/download/"
            "{version}/sccache-{version}-{arch}-unknown-linux-musl.tar.gz"
        ),
        arch=_MUSL_ARCH,
    ),
    "tofu": NativeTool(
        url=(
            "https://github.com/opentofu/opentofu/releases/download/"
            "{version}/tofu_{bare}_linux_{arch}.tar.gz"
        ),
        probe=("version",),
    ),
}

# platform.machine() -> the arch every table entry is keyed on.
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class NativeToolError(ValueError):
    """``test.native_tools`` is malformed or names a tool hyperi-ci cannot install."""


def known_tools() -> list[str]:
    """Every tool name ``test.native_tools`` accepts, sorted."""
    return sorted(name for name, tool in _TOOLS.items() if tool.probe)


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
    known = known_tools()
    unknown = [n for n in names if n not in known]
    if unknown:
        raise NativeToolError(
            f"{CONFIG_KEY} names a tool hyperi-ci cannot install: "
            f"{', '.join(repr(n) for n in unknown)}. "
            f"Known: {', '.join(known)}"
        )
    return names


def _asset_url(name: str, arch: str) -> tuple[str, str]:
    """Return ``name``'s release URL for ``arch`` and the digest key it is checked by.

    Args:
        name: A key of the tool registry.
        arch: ``amd64`` or ``arm64``.

    """
    tool = _TOOLS[name]
    version = tool_version(name)
    key = (tool.arch or {}).get(arch, arch)
    url = tool.url.format(
        version=version,
        bare=version.removeprefix("v"),
        arch=key,
        asset=(tool.asset or {}).get(arch, key),
    )
    return url, key


def _linux_arch() -> str | None:
    """The asset arch for this host, or None off Linux or on an unknown CPU."""
    if sys.platform != "linux":
        return None
    return _ARCH.get(platform.machine().lower())


def _pinned_binary(name: str, arch: str) -> bytes | None:
    """Download ``name`` for ``arch`` and return the binary, or None after logging why.

    A pinned URL is not integrity on its own - a release asset can be deleted
    and re-uploaded under the same tag, and these bytes are exec'd on every
    consumer's CI. The digest covers the raw download, archive or binary,
    before anything is unpacked.
    """
    url, key = _asset_url(name, arch)
    expected = tool_sha256(name, key)
    payload = download_artefact(name, url)
    if payload is None:
        return None

    got = hashlib.sha256(payload).hexdigest()
    if got != expected.strip().lower():
        error(
            f"  {name}: SHA256 mismatch - refusing to install "
            f"(expected {expected}, got {got})"
        )
        return None
    if not url.endswith(".tar.gz"):
        return payload

    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = next(
                (
                    m
                    for m in archive.getmembers()
                    if m.isfile() and Path(m.name).name == name
                ),
                None,
            )
            extracted = archive.extractfile(member) if member else None
            data = extracted.read() if extracted else b""
    except (tarfile.TarError, OSError):
        data = b""
    if not data:
        error(f"  {name}: no '{name}' file in {url}")
        return None
    return data


def install_into(name: str, bin_dir: Path) -> Path | None:
    """Write the pinned ``name`` into ``bin_dir``, replacing any copy there.

    Args:
        name: A key of the tool registry.
        bin_dir: The directory to write the binary into.

    Returns:
        The binary's path, or None after logging why there is none: a host
        other than Linux x86_64 or aarch64, a failed download, a digest
        mismatch, an archive without the binary, or a directory that cannot be
        written.

    """
    arch = _linux_arch()
    if arch is None:
        error(f"  {name}: no pinned build for {sys.platform} {platform.machine()}")
        return None
    data = _pinned_binary(name, arch)
    if data is None:
        return None

    # Written under a temporary name and renamed, so an interrupted install
    # never leaves a truncated binary and a running copy is never overwritten.
    binary = bin_dir / name
    partial = bin_dir / f".{name}.partial"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(data)
        partial.chmod(0o755)
        partial.replace(binary)
    except OSError as exc:
        error(f"  {name}: cannot write {binary}: {exc}")
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        return None
    return binary


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

    bin_dir = cache_dir / "native-tools" / name / f"{tool_version(name)}-{arch}"
    if (bin_dir / name).is_file():
        return bin_dir
    return bin_dir if install_into(name, bin_dir) else None


def ci_binary(name: str) -> str | None:
    """Return ``name`` from PATH, else the pinned build installed on Linux CI.

    The quality-gate rule for a missing tool: a local run uses what the
    developer has and the caller warn-skips when it is absent, while CI gets
    the pinned build. An install also goes on the front of PATH, so a gate
    that runs the tool by name finds it. Returns None off CI or off Linux when
    nothing is on PATH, and after logging why when the install fails.
    """
    exe = shutil.which(name)
    if exe:
        return exe
    if not is_ci() or _linux_arch() is None:
        return None
    info(f"  Installing {name} {tool_version(name)}...")
    bin_dir = install_tool(name)
    if bin_dir is None:
        return None
    os.environ["PATH"] = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
    return str(bin_dir / name)


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
