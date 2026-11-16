# Project:   HyperI CI
# File:      src/hyperi_ci/quality/deprecated_files.py
# Purpose:   Config-driven hygiene nudge for deprecated project files
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Warn about deprecated project files present in a repo; never gates a build.

The file-to-message table is the packaged ``config/deprecated-files.yaml``.
"""

import re
from pathlib import Path

import yaml

from hyperi_ci.common import announce, info

_TABLE_PATH = Path(__file__).resolve().parents[1] / "config" / "deprecated-files.yaml"


def _load_table() -> list[dict]:
    """Load the deprecated-files table, empty list if missing/unparseable."""
    try:
        data = yaml.safe_load(_TABLE_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("files", []) or []
    return [e for e in entries if isinstance(e, dict) and e.get("path")]


_RUNS_SEMANTIC_RELEASE = re.compile(
    r"^\s*(?:-\s*)?run:.*\bsemantic-release\b", re.MULTILINE
)
_CENTRAL_RELEASE = ("setup-semantic-release", "predict-version")


def runs_own_semantic_release(root: Path) -> bool:
    """Whether a workflow runs semantic-release on the repo's own config.

    hyperi-ci's ``setup-semantic-release`` deletes a repo's release config
    before semantic-release runs. A workflow that runs semantic-release without
    it, or without ``predict-version``, which uses it, reads the repo's config.
    """
    for workflow in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8", errors="replace")
        uses_central = any(name in text for name in _CENTRAL_RELEASE)
        if _RUNS_SEMANTIC_RELEASE.search(text) and not uses_central:
            return True
    return False


def scan(project_dir: Path | None = None) -> list[str]:
    """Warn about deprecated files present under ``project_dir``.

    A ``warn`` entry is a ``::warning::`` annotation under GitHub Actions and a
    log line elsewhere; an ``info`` entry is a log line.

    Returns:
        The project-relative paths that fired.
    """
    root = project_dir or Path.cwd()
    fired: list[str] = []
    own_release = runs_own_semantic_release(root)
    for entry in _load_table():
        rel = str(entry["path"])
        if not (root / rel).exists():
            continue
        if entry.get("unless") == "own-semantic-release" and own_release:
            continue
        message = str(entry.get("message") or f"{rel} is deprecated - remove it.")
        level = str(entry.get("level", "warn")).strip().lower()
        if level == "info":
            info(message)
        else:
            announce(f"{rel}: {message}", "hyperi-ci deprecated file")
        fired.append(rel)
    return fired
