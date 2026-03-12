# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/_common.py
# Purpose:   Shared TypeScript/Node utilities
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared utilities for TypeScript language handlers."""

from __future__ import annotations

import json
from pathlib import Path


def detect_package_manager(project_dir: Path | None = None) -> str:
    """Detect which package manager the project uses.

    Priority:
      1. package.json "packageManager" field (authoritative, used by Corepack)
      2. Lock file presence (pnpm-lock.yaml, yarn.lock, package-lock.json)
      3. Default to npm

    Args:
        project_dir: Project root. Defaults to cwd.

    Returns:
        One of: pnpm, yarn, npm
    """
    root = project_dir or Path.cwd()
    pkg = root / "package.json"

    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            pm_raw = data.get("packageManager")
            if isinstance(pm_raw, str) and pm_raw:
                # Format: "yarn@1.22.0", "pnpm@8.0.0", "npm@10.0.0"
                name = pm_raw.split("@")[0].strip().lower()
                if name in ("pnpm", "yarn", "npm"):
                    return name
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback to lock file heuristics
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "package-lock.json").exists():
        return "npm"

    return "npm"
