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
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from hyperi_pylib import logger

_NATIVE_DEPS_DIR = Path(__file__).resolve().parent / "config" / "native-deps"


@dataclass
class DepGroup:
    """A group of related native packages triggered by manifest patterns."""

    name: str
    patterns: list[str]
    manifest_files: list[str]
    dpkg_check: str
    apt_packages: list[str] = field(default_factory=list)


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
