# Project:   HyperI CI
# File:      src/hyperi_ci/native_deps.py
# Purpose:   Detect and install native system dependencies from per-language config
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Detect and install native system dependencies for CI builds.

Reads per-language YAML config from config/native-deps/{language}.yaml,
scans project manifest files for known patterns, and installs missing
apt packages on Linux. No-ops on non-Linux platforms.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from hyperi_pylib import logger

# Ubuntu LTS codenames in reverse chronological order for fallback
_LTS_CODENAMES = ["noble", "jammy", "focal"]

_NATIVE_DEPS_DIR = Path(__file__).resolve().parent / "config" / "native-deps"


@dataclass
class AptRepo:
    """An APT repository to add before installing packages.

    If codename is "auto" (default), the current OS codename is tried first.
    If the repo doesn't support it, LTS codenames are tried in reverse order.
    """

    key_url: str
    keyring: str
    url: str
    codename: str = "auto"
    components: str = "main"


@dataclass
class DepGroup:
    """A group of related native packages triggered by manifest patterns."""

    name: str
    patterns: list[str]
    manifest_files: list[str]
    dpkg_check: str
    apt_packages: list[str] = field(default_factory=list)
    apt_repos: list[AptRepo] = field(default_factory=list)
    dpkg_min_version: str = ""


_DEFAULT_LLVM_VERSION = "22"


def _expand_template_vars(text: str) -> str:
    """Expand ${VAR} placeholders in native-deps YAML.

    Supports a small set of hyperi-ci-controlled variables — keeps the
    YAML readable while letting ops override per environment without
    editing code.

    Recognised variables:
      HYPERCI_LLVM_VERSION  — LLVM/BOLT major version (default: 22)

    Unknown ${VAR} placeholders pass through unchanged so apt-cache
    surfaces a clear "package not found" error instead of a silent
    mis-resolve.
    """
    llvm_version = os.environ.get("HYPERCI_LLVM_VERSION", _DEFAULT_LLVM_VERSION)
    return text.replace("${HYPERCI_LLVM_VERSION}", llvm_version)


def _load_dep_groups(language: str) -> list[DepGroup]:
    """Load dep group definitions for a language from bundled config."""
    config_file = _NATIVE_DEPS_DIR / f"{language}.yaml"
    if not config_file.exists():
        logger.warning(f"No native-deps config for language: {language}")
        return []

    raw = yaml.safe_load(_expand_template_vars(config_file.read_text()))
    if not raw:
        return []

    return [
        DepGroup(
            name=entry["name"],
            patterns=entry["patterns"],
            manifest_files=entry["manifest_files"],
            dpkg_check=entry["dpkg_check"],
            apt_packages=entry.get("apt_packages", []),
            apt_repos=[
                AptRepo(
                    key_url=r["key_url"],
                    keyring=r["keyring"],
                    url=r["url"],
                    codename=r.get("codename", "auto"),
                    components=r.get("components", "main"),
                )
                for r in entry.get("apt_repos", [])
            ],
            dpkg_min_version=entry.get("dpkg_min_version", ""),
        )
        for entry in raw
    ]


def _read_manifests(project_dir: Path, manifest_files: list[str]) -> str:
    """Read all manifest files, concatenating their content for pattern matching."""
    content_parts: list[str] = []
    for filename in manifest_files:
        manifest_path = project_dir / filename
        if manifest_path.exists():
            content_parts.append(manifest_path.read_text())
    return "\n".join(content_parts)


def _patterns_match(content: str, patterns: list[str]) -> bool:
    """Return True if any pattern appears as a substring in content."""
    return any(pattern in content for pattern in patterns)


def _get_os_codename() -> str:
    """Get the current OS codename via lsb_release."""
    result = subprocess.run(
        ["lsb_release", "-cs"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _repo_has_codename(repo_url: str, codename: str) -> bool:
    """Check if the repo has a Release file for the given codename."""
    url = f"{repo_url.rstrip('/')}/dists/{codename}/Release"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 — URL constructed from known APT repo
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _resolve_codename(repo: AptRepo) -> str:
    """Resolve the codename to use for an APT repo.

    If codename is explicit, uses it directly. If "auto", tries the current
    OS codename first, then falls back through LTS codenames.
    """
    if repo.codename != "auto":
        return repo.codename

    os_codename = _get_os_codename()
    if os_codename and _repo_has_codename(repo.url, os_codename):
        logger.info(f"Repo {repo.url} supports current codename: {os_codename}")
        return os_codename

    for lts in _LTS_CODENAMES:
        if lts == os_codename:
            continue
        if _repo_has_codename(repo.url, lts):
            logger.info(
                f"Repo {repo.url} does not support {os_codename!r}, "
                f"using fallback: {lts}"
            )
            return lts

    logger.warning(f"No supported codename found for {repo.url}")
    return os_codename or _LTS_CODENAMES[0]


def _get_dpkg_arch() -> str:
    """Get the current dpkg architecture (amd64, arm64, etc.)."""
    result = subprocess.run(
        ["dpkg", "--print-architecture"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "amd64"


def _repo_already_configured(repo_url: str, codename: str) -> Path | None:
    """Check if any existing APT sources file references this repo.

    Self-hosted runners may pre-configure apt.llvm.org (or other upstream
    repos) under a different filename than ours. To avoid duplicate
    entries, scan all files in /etc/apt/sources.list.d/ and the main
    /etc/apt/sources.list for a line that matches our url + codename.

    Returns the path of the first matching file, or None if not found.
    Match is substring-based on `url + " " + codename` so variations in
    `[signed-by=...]` options, arch flags, or components don't cause
    false negatives.
    """
    # Normalise scheme — pre-provisioned runners often use `http://` for
    # apt.llvm.org (their Dockerfile does) while we write `https://`.
    # Match on the scheme-less path so either form is detected.
    url_stripped = repo_url.rstrip("/")
    path_only = url_stripped.split("://", 1)[-1]  # e.g. "apt.llvm.org/noble"

    candidates = [Path("/etc/apt/sources.list")]
    sources_dir = Path("/etc/apt/sources.list.d")
    if sources_dir.is_dir():
        candidates.extend(sources_dir.glob("*.list"))
        candidates.extend(sources_dir.glob("*.sources"))

    for path in candidates:
        try:
            content = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        # Match on scheme-less URL path + codename. Covers both the
        # one-line `deb` format and deb822-style `.sources` files.
        if path_only in content and codename in content:
            return path
    return None


def _add_apt_repo(repo: AptRepo) -> int:
    """Add a GPG key and APT sources entry for a repo. Returns exit code.

    Idempotent on three levels:
      1. GPG keyring: skips download if keyring file already present.
      2. Exact sources.list match: skips write if our file already has
         the exact same line.
      3. Cross-file duplicate detection: skips write if ANY other apt
         source file already references the same url + codename — this
         handles self-hosted runners where admins have pre-configured
         upstream repos under a different filename.
    """
    keyring_path = Path(repo.keyring)
    if keyring_path.exists():
        logger.info(f"APT keyring already exists: {repo.keyring}")
    else:
        logger.info(f"Adding APT key from {repo.key_url}")
        # Download key and dearmor into keyring
        dl = subprocess.run(
            ["curl", "-fsSL", repo.key_url],
            capture_output=True,
        )
        if dl.returncode != 0:
            logger.error(f"Failed to download APT key from {repo.key_url}")
            return dl.returncode

        dearmor = subprocess.run(
            [
                "sudo",
                "gpg",
                "--batch",
                "--yes",
                "--dearmor",
                "-o",
                repo.keyring,
            ],
            input=dl.stdout,
        )
        if dearmor.returncode != 0:
            logger.error(f"Failed to dearmor APT key to {repo.keyring}")
            return dearmor.returncode

    codename = _resolve_codename(repo)
    arch = _get_dpkg_arch()
    sources_line = (
        f"deb [signed-by={repo.keyring} arch={arch}] "
        f"{repo.url} {codename} {repo.components}"
    )

    # Cross-file check: if another sources file already references this
    # repo (pre-configured by runner admin), skip to avoid duplicates.
    existing = _repo_already_configured(repo.url, codename)
    if existing is not None:
        logger.info(f"APT source for {repo.url} {codename} already present in {existing}")
        return 0

    # Derive a stable filename from the keyring name
    sources_name = keyring_path.stem + ".list"
    sources_path = Path("/etc/apt/sources.list.d") / sources_name

    if sources_path.exists() and sources_path.read_text().strip() == sources_line:
        logger.info(f"APT source already configured: {sources_path}")
        return 0

    logger.info(f"Adding APT source: {sources_line}")
    result = subprocess.run(
        ["sudo", "tee", str(sources_path)],
        input=sources_line.encode(),
        capture_output=True,
    )
    return result.returncode


def _is_dpkg_installed(package: str, min_version: str = "") -> bool:
    """Return True if a dpkg package is installed (and meets min version)."""
    result = subprocess.run(
        ["dpkg", "-s", package],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    if not min_version:
        return True

    for line in result.stdout.splitlines():
        if line.startswith("Version:"):
            installed = line.split(":", 1)[1].strip()
            cmp = subprocess.run(
                ["dpkg", "--compare-versions", installed, "ge", min_version],
                capture_output=True,
            )
            if cmp.returncode != 0:
                logger.info(f"{package} {installed} installed but < {min_version}")
                return False
            return True
    return False


def _apt_install(packages: list[str]) -> int:
    """Run apt-get update then install packages. Returns exit code."""
    update = subprocess.run(["sudo", "apt-get", "update"])
    if update.returncode != 0:
        logger.warning("apt-get update failed — continuing anyway")

    install = subprocess.run(
        [
            "sudo",
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            *packages,
        ]
    )
    return install.returncode


def install_native_deps(language: str, project_dir: Path | None = None) -> int:
    """Detect and install native system deps for the given language.

    Args:
        language: Language identifier (rust, typescript, golang, python).
        project_dir: Project root. Defaults to cwd.

    Returns:
        0 on success, non-zero on failure.
    """
    cwd = project_dir or Path.cwd()

    if platform.system() != "Linux":
        logger.info(f"Skipping native deps on {platform.system()}")
        return 0

    dep_groups = _load_dep_groups(language)
    if not dep_groups:
        logger.info(f"No native dep groups defined for {language}")
        return 0

    needed: list[DepGroup] = []
    for group in dep_groups:
        content = _read_manifests(cwd, group.manifest_files)
        if not content:
            continue

        if _patterns_match(content, group.patterns):
            if _is_dpkg_installed(group.dpkg_check, group.dpkg_min_version):
                logger.info(f"[{group.name}] already installed ({group.dpkg_check})")
            else:
                logger.info(f"[{group.name}] needs install: {group.apt_packages}")
                needed.append(group)

    if not needed:
        logger.info(f"All native deps satisfied for {language}")
        return 0

    # Add any custom APT repos before installing
    for group in needed:
        for repo in group.apt_repos:
            rc = _add_apt_repo(repo)
            if rc != 0:
                logger.error(f"Failed to add APT repo for [{group.name}]")
                return rc

    # Collect all packages across needed groups and install in one apt call.
    all_packages: list[str] = []
    seen: set[str] = set()
    for group in needed:
        for pkg in group.apt_packages:
            if pkg not in seen:
                all_packages.append(pkg)
                seen.add(pkg)

    logger.info(f"Installing native packages: {all_packages}")
    rc = _apt_install(all_packages)
    if rc != 0:
        logger.error(f"apt-get install failed (exit {rc})")
        return rc

    # Universal per-language tooling (cargo / npm / pip / go-install)
    rc = _install_language_tools(language)
    if rc != 0:
        return rc

    logger.info(f"Native deps installed for {language}")
    return 0


# ---------------------------------------------------------------------------
# Universal language tooling
# ---------------------------------------------------------------------------
#
# Install-once-per-runner tooling keyed by language. Runs unconditionally
# after apt deps. Philosophy: if a CI stage (quality, test, build, release)
# might need a tool, install it at setup time instead of just-in-time. The
# "which stage uses it?" gating would be brittle + slow on opt-in features
# like Rust Tier 2 PGO.
#
# Each entry:
#   name:         Human label for logs
#   binary:       Executable to probe for "already installed?"
#   bin_dir:      Directory where the installer drops binaries (ensured on PATH)
#   installer:    Shell tokens that install the tool (prepended to cargo/npm/etc)
#   args:         Arguments passed after the installer tokens
#
# Non-fatal: failures log a warning and the function continues. Downstream
# stages that depend on the tool handle the missing-tool case themselves
# (Rust Tier 2 falls back to plain release, etc.).


@dataclass(frozen=True)
class LanguageTool:
    name: str
    binary: str
    bin_dir: str  # relative to $HOME
    installer: list[str]
    args: list[str]


_LANGUAGE_TOOLS: dict[str, list[LanguageTool]] = {
    "rust": [
        LanguageTool(
            name="cargo-pgo",
            binary="cargo-pgo",
            bin_dir=".cargo/bin",
            installer=["cargo", "install"],
            args=["cargo-pgo", "--locked"],
        ),
    ],
    # Populate as concrete needs appear. The abstraction is in place;
    # adding a Python tool (e.g. 'pip-audit' if a stage requires it) is
    # one entry below. Don't over-populate preemptively — only list tools
    # the CI pipeline actually runs.
    "python": [],
    "typescript": [],
    "golang": [],
}


def _install_language_tools(language: str) -> int:
    """Install per-language tooling (cargo / npm / pip / go-install tools).

    Runs universally (not pattern-gated) — tools listed here may be
    required by any CI stage (quality, test, build, release). Gating by
    stage would be fragile on opt-in features like Rust Tier 2 PGO.

    Non-fatal: install failure logs a warning and continues. Downstream
    stages that depend on the tool handle missing-tool fallback.

    No-op on non-Linux — CI stages that need these tools only run on
    Linux runners in our current pipelines.
    """
    tools = _LANGUAGE_TOOLS.get(language, [])
    if not tools:
        return 0
    if platform.system() != "Linux":
        return 0

    import os
    import shutil

    for tool in tools:
        # Ensure the tool's install dir is on PATH before probe + install
        bin_dir = Path.home() / tool.bin_dir
        current_path = os.environ.get("PATH", "")
        if str(bin_dir) not in current_path.split(os.pathsep):
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{current_path}"

        if shutil.which(tool.binary) or (bin_dir / tool.binary).exists():
            logger.info(f"[{tool.name}] already installed")
            continue

        cmd = [*tool.installer, *tool.args]
        logger.info(f"Installing {tool.name}: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            logger.warning(
                f"[{tool.name}] install failed (exit {result.returncode}) — "
                "dependent CI stages will handle the missing tool"
            )
            # Non-fatal: downstream has graceful fallback
    return 0


def print_needed(language: str, project_dir: Path | None = None) -> None:
    """Print which dep groups would be triggered (dry-run helper)."""
    cwd = project_dir or Path.cwd()
    dep_groups = _load_dep_groups(language)

    for group in dep_groups:
        content = _read_manifests(cwd, group.manifest_files)
        matched = content and _patterns_match(content, group.patterns)
        installed = (
            _is_dpkg_installed(group.dpkg_check)
            if platform.system() == "Linux"
            else None
        )
        status = (
            "matched+installed"
            if matched and installed
            else "matched+needed"
            if matched and not installed
            else "not matched"
        )
        print(f"  {group.name}: {status}", file=sys.stderr)
