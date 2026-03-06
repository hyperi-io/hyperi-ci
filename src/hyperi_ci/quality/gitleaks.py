# Project:   HyperI CI
# File:      src/hyperi_ci/quality/gitleaks.py
# Purpose:   Gitleaks secret scanning (cross-language)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Gitleaks secret scanning.

Scans repository git history for committed secrets. Runs before
language-specific quality checks on every project.

Ported from old CI: ci/scripts/core/gitleaks.sh
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, success, warn
from hyperi_ci.config import CIConfig

_GITLEAKS_VERSION = "v8.21.2"


def _install_gitleaks() -> bool:
    """Install gitleaks binary on Linux CI runners.

    Returns:
        True if gitleaks is available after install attempt.
    """
    if shutil.which("gitleaks"):
        return True

    if not is_ci():
        return False

    if sys.platform != "linux":
        warn("  gitleaks auto-install only supported on Linux CI")
        return False

    import platform

    arch = "x64" if platform.machine() in ("x86_64", "AMD64") else "arm64"
    version_num = _GITLEAKS_VERSION.lstrip("v")
    url = (
        f"https://github.com/gitleaks/gitleaks/releases/download/"
        f"{_GITLEAKS_VERSION}/gitleaks_{version_num}_linux_{arch}.tar.gz"
    )

    info(f"  Installing gitleaks {_GITLEAKS_VERSION}...")
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            ["curl", "-sSL", url],
            capture_output=True,
        )
        if result.returncode != 0:
            error("  Failed to download gitleaks")
            return False

        tar_path = Path(tmp) / "gitleaks.tar.gz"
        tar_path.write_bytes(result.stdout)
        subprocess.run(
            ["tar", "xzf", str(tar_path), "-C", tmp],
            check=True,
        )
        bin_path = Path(tmp) / "gitleaks"
        if bin_path.exists():
            dest = Path("/usr/local/bin/gitleaks")
            subprocess.run(
                ["sudo", "mv", str(bin_path), str(dest)],
                check=True,
            )
            subprocess.run(["sudo", "chmod", "+x", str(dest)], check=True)

    return shutil.which("gitleaks") is not None


def _find_config() -> str | None:
    """Find gitleaks config file in project."""
    for path in (".gitleaks.toml", "ci/.gitleaks.toml"):
        if Path(path).exists():
            return path
    return None


def run(config: CIConfig) -> int:
    """Run gitleaks secret scanning.

    Args:
        config: Merged CI configuration.

    Returns:
        Exit code (0 = success).
    """
    mode = str(config.get("quality.gitleaks", "blocking"))
    if mode == "disabled":
        info("  gitleaks: disabled")
        return 0

    if not _install_gitleaks():
        if is_ci():
            if mode == "blocking":
                error("  gitleaks: not installed (required)")
                return 1
            warn("  gitleaks: not installed (skipping)")
            return 0
        warn("  gitleaks: not installed — skipping secret scanning")
        warn(
            "  Install: brew install gitleaks (macOS) or go install github.com/gitleaks/gitleaks/v8@latest"
        )
        return 0

    # Build command — scan current branch only
    cmd: list[str] = ["gitleaks", "detect", "--source", ".", "--verbose"]

    # Restrict to current branch to avoid scanning unmerged branches
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip() if result.returncode == 0 else "HEAD"
    cmd.extend(["--log-opts", branch])

    # Use custom config if present
    cfg = _find_config()
    if cfg:
        cmd.extend(["--config", cfg])

    env = dict(os.environ)
    # GITLEAKS_LICENSE key if available (org secret)
    gitleaks_key = os.environ.get("GITLEAKS_GH_ACTIONS_KEY")
    if gitleaks_key:
        env["GITLEAKS_LICENSE"] = gitleaks_key

    info("  gitleaks: scanning for secrets...")
    scan = subprocess.run(cmd, env=env)

    if scan.returncode == 0:
        success("  gitleaks: no secrets detected")
        return 0

    if mode == "warn":
        warn("  gitleaks: secrets detected (non-blocking)")
        return 0

    error("  gitleaks: secrets detected in repository!")
    error("  Review output above and remove/rotate exposed secrets.")
    error("  For false positives, add them to .gitleaks.toml")
    return 1
