# Project:   HyperI CI
# File:      src/hyperi_ci/detect.py
# Purpose:   Auto-detect project language from file markers
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Auto-detect project language from file markers.

Override detection:
  1. Environment variable: HYPERI_CI_LANGUAGE=rust
  2. Config file: .hyperi-ci.yaml with 'language: rust'
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

LANGUAGE_MARKERS: dict[str, list[str]] = {
    "python": ["pyproject.toml", "setup.py", "requirements.txt", "setup.cfg"],
    "typescript": ["tsconfig.json"],
    "javascript": ["package.json"],
    "rust": ["Cargo.toml"],
    "golang": ["go.mod", "go.sum"],
    "bash": [],
}


def _get_override_language(project_dir: Path | None = None) -> str | None:
    """Check for language override from environment or config file."""
    env_lang = os.environ.get("HYPERI_CI_LANGUAGE") or os.environ.get(
        "HYPERSEC_CI_LANGUAGE",
    )
    if env_lang:
        return env_lang.lower().strip()

    project_dir = project_dir or Path.cwd()
    for config_name in (
        ".hyperi-ci.yaml",
        ".hyperi-ci.yml",
        ".hypersec-ci.yaml",
        ".hypersec-ci.yml",
    ):
        config_file = project_dir / config_name
        if not config_file.exists():
            continue
        try:
            with open(config_file) as f:
                config = yaml.safe_load(f)
                if config and isinstance(config, dict):
                    lang = config.get("language")
                    if lang and str(lang).lower().strip() != "none":
                        return str(lang).lower().strip()
        except Exception:
            pass

    return None


def _has_bats_tests(project_dir: Path | None = None) -> bool:
    """Check if project has BATS test files."""
    project_dir = project_dir or Path.cwd()
    for d in (project_dir / "tests", project_dir / "test"):
        if d.exists() and d.is_dir() and list(d.glob("*.bats")):
            return True
    return False


def detect_language(project_dir: Path | None = None) -> str | None:
    """Detect primary language from project files.

    Args:
        project_dir: Directory to scan. Defaults to cwd.

    Returns:
        Language name (lowercase) or None if not detected.

    """
    project_dir = project_dir or Path.cwd()

    override = _get_override_language(project_dir)
    if override:
        return override

    for lang, markers in LANGUAGE_MARKERS.items():
        if markers and any((project_dir / marker).exists() for marker in markers):
            if lang == "javascript" and (project_dir / "tsconfig.json").exists():
                continue
            return lang

    if _has_bats_tests(project_dir):
        return "bash"

    return None
