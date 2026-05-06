# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/build.py
# Purpose:   Golang build handler with cross-compilation and version injection
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang build handler.

Builds Go projects with ldflags version injection, cross-compilation,
binary stripping, and SHA256 checksums. Output follows the naming
convention: {binary}-{os}-{arch}[.exe]
Version is in the R2/release path, not the filename.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from hyperi_ci.common import error, group, info, sanitize_ref_name, success
from hyperi_ci.config import CIConfig
from hyperi_ci.languages._build_common import (
    generate_checksums as _generate_checksums,
)
from hyperi_ci.languages._build_common import (
    human_size as _human_size,
)

_TARGET_SHORTCUTS = {
    "all": [
        "linux/amd64",
        "linux/arm64",
        "darwin/amd64",
        "darwin/arm64",
        "windows/amd64",
    ],
    "linux": ["linux/amd64", "linux/arm64"],
    "darwin": ["darwin/amd64", "darwin/arm64"],
    "windows": ["windows/amd64", "windows/arm64"],
}


def _detect_binary_name() -> str:
    """Detect binary name from go.mod module path."""
    result = subprocess.run(
        ["go", "list", "-m"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        module_path = result.stdout.strip()
        return module_path.rsplit("/", 1)[-1]
    return Path.cwd().name


def _detect_main_package(binary_name: str) -> str:
    """Auto-detect the main package to build.

    Priority: GO_MAIN_PKG env > cmd/{binary}/ > single cmd/ subdir > .
    """
    explicit = os.environ.get("GO_MAIN_PKG", "")
    if explicit:
        return explicit

    cmd_specific = Path(f"cmd/{binary_name}")
    if cmd_specific.is_dir():
        return f"./cmd/{binary_name}"

    cmd_dir = Path("cmd")
    if cmd_dir.is_dir():
        subdirs = [d for d in cmd_dir.iterdir() if d.is_dir()]
        if len(subdirs) == 1:
            return f"./cmd/{subdirs[0].name}"

    return "."


def _detect_version() -> str:
    """Detect version from VERSION file, env vars, or fallback to "dev".

    Priority: VERSION file (semantic-release) > explicit env > "dev".
    GITHUB_REF_NAME is deliberately excluded — during the publish job it is
    the branch name (e.g. "release"), not the tag.
    """
    version_file = Path("VERSION")
    if version_file.exists():
        val = version_file.read_text().strip()
        if val:
            return f"v{val}" if not val.startswith("v") else val

    for var in ("GO_VERSION", "CI_COMMIT_TAG"):
        val = os.environ.get(var, "")
        if val:
            return sanitize_ref_name(val)
    return "dev"


def _build_ldflags(version: str, version_pkg: str) -> str:
    """Build ldflags string with version injection.

    -s strips symbol table, -w strips DWARF debug info.
    Version/commit/build time are injected via -X if version_pkg is set.
    """
    commit_result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = (
        commit_result.stdout.strip() if commit_result.returncode == 0 else "unknown"
    )
    build_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    base = os.environ.get("GO_LDFLAGS", "-s -w")

    if version_pkg:
        base += f" -X '{version_pkg}.Version={version}'"
        base += f" -X '{version_pkg}.Commit={commit}'"
        base += f" -X '{version_pkg}.BuildTime={build_time}'"

    return base


def _expand_targets(targets: list[str]) -> list[str]:
    """Expand target shortcuts like 'all', 'linux' into os/arch pairs."""
    expanded: list[str] = []
    for target in targets:
        if target in _TARGET_SHORTCUTS:
            expanded.extend(_TARGET_SHORTCUTS[target])
        else:
            expanded.append(target)
    return expanded


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Golang build.

    Args:
        config: Merged CI configuration.
        extra_env: Additional env vars (GO_VERSION_PKG, GO_MAIN_PKG, etc).

    Returns:
        Exit code (0 = success).

    """
    extra = extra_env or {}
    info("Building Golang project...")

    targets_raw = config.get("build.golang.targets", ["linux/amd64"])
    if isinstance(targets_raw, str):
        targets_raw = [t.strip() for t in targets_raw.split(",") if t.strip()]
    targets = _expand_targets(targets_raw)

    cgo = config.get("build.golang.cgo", False)
    version_pkg = extra.get("GO_VERSION_PKG", "")
    binary_name = extra.get("GO_BINARY_NAME", "") or _detect_binary_name()
    version = _detect_version()
    main_pkg = _detect_main_package(binary_name)
    ldflags = _build_ldflags(version, version_pkg)

    output_dir = Path("dist")
    output_dir.mkdir(parents=True, exist_ok=True)

    info(f"Binary: {binary_name}")
    info(f"Version: {version}")
    info(f"Main package: {main_pkg}")
    info(f"Targets: {', '.join(targets)}")
    if version_pkg:
        info(f"Version package: {version_pkg}")

    for target in targets:
        parts = target.split("/")
        if len(parts) != 2:
            error(f"Invalid Go target: {target}")
            return 1

        goos, goarch = parts
        output_name = f"{binary_name}-{goos}-{goarch}"
        if goos == "windows":
            output_name += ".exe"
        output_path = output_dir / output_name

        with group(f"Build: {goos}/{goarch}"):
            env = {
                **os.environ,
                "GOOS": goos,
                "GOARCH": goarch,
                "CGO_ENABLED": "1" if cgo else "0",
            }

            cmd = [
                "go",
                "build",
                "-ldflags",
                ldflags,
                "-o",
                str(output_path),
                main_pkg,
            ]

            build_tags = extra.get("GO_BUILD_TAGS", "")
            if build_tags:
                cmd.insert(2, "-tags")
                cmd.insert(3, build_tags)

            result = subprocess.run(cmd, env=env)
            if result.returncode != 0:
                error(f"Build failed for {goos}/{goarch}")
                return result.returncode

            if output_path.exists():
                size = _human_size(output_path.stat().st_size)
                info(f"  Created: {output_path.name} ({size})")
            success(f"Built: {goos}/{goarch}")

    with group("Build summary"):
        for f in sorted(output_dir.iterdir()):
            if f.is_file() and f.name != "checksums.sha256":
                info(f"  {f.name} ({_human_size(f.stat().st_size)})")

        _generate_checksums(output_dir)

    success("Build complete")
    return 0
