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


def _load_dep_groups(language: str) -> list[DepGroup]:
    """Load dep group definitions for a language from bundled config."""
    config_file = _NATIVE_DEPS_DIR / f"{language}.yaml"
    if not config_file.exists():
        logger.warning(f"No native-deps config for language: {language}")
        return []

    raw = yaml.safe_load(config_file.read_text())
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
        with urllib.request.urlopen(req, timeout=10) as resp:
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


def _add_apt_repo(repo: AptRepo) -> int:
    """Add a GPG key and APT sources entry for a repo. Returns exit code."""
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
    sources_line = (
        f"deb [signed-by={repo.keyring} arch=amd64] "
        f"{repo.url} {codename} {repo.components}"
    )

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


def _is_dpkg_installed(package: str) -> bool:
    """Return True if a dpkg package is installed."""
    result = subprocess.run(
        ["dpkg", "-s", package],
        capture_output=True,
    )
    return result.returncode == 0


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
            if _is_dpkg_installed(group.dpkg_check):
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

    logger.info(f"Native deps installed for {language}")
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
