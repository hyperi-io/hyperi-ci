# Project:   HyperI CI
# File:      src/hyperi_ci/schedule.py
# Purpose:   Per-repo nightly cron for the scaffolded ci.yml
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Nightly full-tier schedule for a consumer's ci.yml.

The plan job runs the full tier on a ``schedule`` event, but only a ``schedule:``
trigger in the caller creates one. Each repo hashes its name to a minute and hour
inside ``test.full.schedule_window_utc``, so the fleet does not start together.
Minutes :00 and :30 are skipped because most other crons on GitHub land there.
"""

import hashlib
import re
from enum import StrEnum
from pathlib import Path

import yaml

from hyperi_ci.config import ConfigError, load_config, shipped_default
from hyperi_ci.gate_audit import triggers_on_schedule

_MINUTES = tuple(m for m in range(60) if m not in (0, 30))
_WINDOW = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_ON_KEY = re.compile(r"""^(?:"on"|'on'|on)\s*:\s*(?:#.*)?$""")


class ScheduleResult(StrEnum):
    """What :func:`ensure_schedule` did to a caller workflow."""

    ADDED = "added"
    PRESENT = "present"
    UNSUPPORTED = "unsupported"


def schedule_cron(repo_name: str, window: str) -> str:
    """Return the repo's daily cron, stable for a name and a window.

    Args:
        repo_name: Name the slot is hashed from.
        window: UTC hours as ``"<first>-<last>"``, both inclusive.

    Raises:
        ValueError: The window is not two ordered hours, 0-23.

    """
    match = _WINDOW.match(window.strip())
    if not match or not 0 <= int(match[1]) <= int(match[2]) <= 23:
        msg = f"test.full.schedule_window_utc must be like '14-18', got {window!r}"
        raise ValueError(msg)
    hours = range(int(match[1]), int(match[2]) + 1)
    digest = hashlib.sha256(repo_name.encode("utf-8")).digest()
    slot = int.from_bytes(digest[:8], "big") % (len(hours) * len(_MINUTES))
    return f"{_MINUTES[slot % len(_MINUTES)]} {hours[slot // len(_MINUTES)]} * * *"


def project_cron(project_dir: Path, repo_name: str) -> str:
    """Return the cron for a project, with the window from its merged config."""
    try:
        config = load_config(project_dir=project_dir, reload=True, report_removed=False)
        window = str(config.setting("test.full.schedule_window_utc"))
    except ConfigError:
        # init skips a malformed .hyperi-ci.yaml rather than fail on it.
        window = str(shipped_default("test.full.schedule_window_utc"))
    return schedule_cron(repo_name, window)


def schedule_block(cron: str, indent: str = "  ") -> str:
    """Render the ``schedule:`` trigger, indented to sit under ``on:``."""
    return f"{indent}schedule:\n{indent}  - cron: '{cron}'\n"


def ensure_schedule(workflow_path: Path, cron: str) -> ScheduleResult:
    """Add a ``schedule:`` trigger to a caller workflow that has none.

    Edits the text, so comments and every other trigger survive. An inline
    ``on: [push]`` or a file that does not parse is left alone.

    Returns:
        ADDED when the file changed, PRESENT when it already schedules,
        UNSUPPORTED when the trigger block cannot be edited safely.

    """
    text = workflow_path.read_text(encoding="utf-8", errors="replace")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return ScheduleResult.UNSUPPORTED
    if not isinstance(doc, dict):
        return ScheduleResult.UNSUPPORTED
    if triggers_on_schedule(doc):
        return ScheduleResult.PRESENT
    lines = text.splitlines(keepends=True)
    on_at = next((i for i, ln in enumerate(lines) if _ON_KEY.match(ln.rstrip())), -1)
    if on_at < 0 or not isinstance(doc.get("on", doc.get(True)), dict):
        return ScheduleResult.UNSUPPORTED
    body = (ln for ln in lines[on_at + 1 :] if ln.strip() and ln.lstrip()[0] != "#")
    child = next(body, "  ")
    lines.insert(
        on_at + 1, schedule_block(cron, child[: len(child) - len(child.lstrip())])
    )
    workflow_path.write_text("".join(lines), encoding="utf-8", newline="\n")
    return ScheduleResult.ADDED
