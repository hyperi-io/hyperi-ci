# Project:   HyperI CI
# File:      src/hyperi_ci/deps/renovate.py
# Purpose:   Which present surfaces the repo's Renovate config never sees
# Origin:    Derek's deps automation scripts, merged into hyperi-ci now they are
#            mature enough for people (and hyperi-ai's /deps) to use directly
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Present surfaces the repo's Renovate config never sees.

A configured Renovate does not mean a surface is covered. A present surface is
uncovered when the repo has no Renovate config, no manager exists for it
(tox.ini, noxfile, .hyperi-ci.yaml), its manager is missing from a non-empty
``enabledManagers``, or its manager is ``inert``, which reads as covered.
"""

import json
from pathlib import Path

from hyperi_ci.deps.surfaces import ABSENT, INERT

# Renovate's own lookup order, first match wins. package.json's deprecated
# "renovate" key is not listed.
# https://docs.renovatebot.com/configuration-options/
CONFIG_NAMES: tuple[str, ...] = (
    "renovate.json",
    "renovate.jsonc",
    "renovate.json5",
    ".github/renovate.json",
    ".github/renovate.jsonc",
    ".github/renovate.json5",
    ".gitlab/renovate.json",
    ".gitlab/renovate.jsonc",
    ".gitlab/renovate.json5",
    ".renovaterc",
    ".renovaterc.json",
    ".renovaterc.jsonc",
    ".renovaterc.json5",
)

_COMMENTED_SUFFIXES = (".json5", ".jsonc")


def _strip_comments_and_trailing_commas(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments and trailing commas outside strings.

    Covers the JSON5 features Renovate configs use. Single-quoted strings and
    unquoted keys are not handled, and fail the parse.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
        elif ch == ",":
            j = i + 1
            while j < n:
                if text[j].isspace():
                    j += 1
                elif text.startswith("//", j):
                    while j < n and text[j] != "\n":
                        j += 1
                elif text.startswith("/*", j):
                    end = text.find("*/", j + 2)
                    j = n if end == -1 else end + 2
                else:
                    break
            if j < n and text[j] in "}]":
                i += 1
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _load_config(path: Path) -> object:
    """Parse a Renovate config, tolerating comments in ``.json5``/``.jsonc``."""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if path.suffix not in _COMMENTED_SUFFIXES:
            raise
        return json.loads(_strip_comments_and_trailing_commas(text))


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
            raw = _load_config(config_path)
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
