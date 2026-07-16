# Project:   HyperI CI
# File:      src/hyperi_ci/quality/deprecated_files.py
# Purpose:   Config-driven hygiene nudge for deprecated project files
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Warn about deprecated project files present in a repo.

A table-driven tidy-up nudge: the file->message table is a packaged config
(``config/deprecated-files.yaml``), so adding a newly-retired file is a data
edit, not a code change. Runs on ``hyperi-ci check`` (local pre-push) and in
CI. Non-fatal by design - it recommends removal, it never gates a build.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hyperi_ci.common import info, is_ci, warn

# config/ is a sibling of quality/ inside the package (both under hyperi_ci/).
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


def scan(project_dir: Path | None = None) -> list[str]:
    """Warn about deprecated files present under ``project_dir``.

    For each table entry whose path exists, emit a non-fatal nudge - a
    ``warn`` also prints a GitHub ``::warning::`` annotation in CI so it
    escapes the folded log group and lands in the run summary. Returns the
    project-relative paths that fired (for callers / tests). Never raises and
    never fails a build: it is a recommendation, not a gate.
    """
    root = project_dir or Path.cwd()
    fired: list[str] = []
    for entry in _load_table():
        rel = str(entry["path"])
        if not (root / rel).exists():
            continue
        message = str(entry.get("message") or f"{rel} is deprecated - remove it.")
        level = str(entry.get("level", "warn")).strip().lower()
        if level == "info":
            info(message)
        else:
            warn(message)
            if is_ci():
                print(f"::warning title=hyperi-ci deprecated file::{rel}: {message}")
        fired.append(rel)
    return fired
