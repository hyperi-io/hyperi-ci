# Project:   HyperI CI
# File:      src/hyperi_ci/deps/renovate.py
# Purpose:   Which present surfaces the repo's Renovate config never sees
# Origin:    Derek's deps automation scripts, merged into hyperi-ci now they are
#            mature enough for people (and hyperi-ai's /deps) to use directly
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The prevention/remediation boundary, made concrete.

Renovate is REMEDIATION: it runs on the forge, after the fact, and raises PRs
for what has already gone stale. ``hyperi-ci deps`` is PREVENTION: it runs
locally, before the change lands, and says what you are about to leave stale.
Neither replaces the other, and "Renovate is configured" must never be read as
"the surfaces are covered".

This module draws that line for one repo. Three ways a present surface goes
uncovered, all reported the same:

- no Renovate manager exists at all (tox.ini, noxfile, an unmarked container
  tag in test source, .hyperi-ci.yaml);
- a manager exists but is not in a non-empty ``enabledManagers``;
- the manager is enabled and INERT -- it matched files and extracted nothing,
  or it ships with empty default file patterns (``kubernetes``,
  ``pip-compile``). That last one is the dangerous case, because it reads as
  covered.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyperi_ci.deps.surfaces import ABSENT, INERT

# Renovate config filenames, in the order Renovate itself resolves them.
CONFIG_NAMES: tuple[str, ...] = (
    "renovate.json",
    "renovate.json5",
    ".renovaterc",
    ".renovaterc.json",
    ".github/renovate.json",
    ".github/renovate.json5",
)


def gaps(root: Path, scan_result: dict) -> dict:
    """List the present surfaces no enabled Renovate manager will ever see.

    Args:
        root: Repository root.
        scan_result: Output of :func:`hyperi_ci.deps.surfaces.scan`.

    Returns:
        Config path (or None), the declared ``enabledManagers`` (or None when
        unset, meaning Renovate's own defaults), and one record per uncovered
        surface with the reason.

    """
    root = Path(root).resolve()
    config_path: Path | None = None
    for name in CONFIG_NAMES:
        candidate = root / name
        if candidate.is_file():
            config_path = candidate
            break

    enabled: list[str] | None = None
    if config_path is not None:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        value = raw.get("enabledManagers") if isinstance(raw, dict) else None
        if isinstance(value, list):
            enabled = [str(item) for item in value]

    uncovered: list[dict] = []
    for record in scan_result["surfaces"]:
        if record["state"] == ABSENT:
            continue
        manager = record["renovate_manager"]
        if config_path is None:
            reason = "no renovate config in this repo"
        elif manager is None:
            reason = "no renovate manager exists for this surface"
        elif enabled and manager not in enabled:
            reason = f"manager '{manager}' is not in enabledManagers"
        elif record["state"] == INERT:
            reason = f"manager '{manager}' matched nothing extractable (inert)"
        else:
            continue
        uncovered.append(
            {
                "id": record["id"],
                "label": record["label"],
                "kind": record["kind"],
                "state": record["state"],
                "renovate_manager": manager,
                "reason": reason,
                "detail": record["gap"] or record["caveat"],
            }
        )
    return {
        "root": str(root),
        "config": config_path.relative_to(root).as_posix() if config_path else None,
        "enabled_managers": enabled,
        "uncovered": uncovered,
    }
