#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/audit-config-keys.py
# Purpose:   Fail when defaults.yaml declares a key nothing reads
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Every key we document as configurable must actually do something.

A key in ``defaults.yaml`` is a promise: set it, and behaviour changes. Twenty-
nine of them turned out to be promises nothing kept -- ``build.python.nuitka.*``,
``runners.*``, ``standards.*`` -- documented and scaffolded for years while no
code read them. A test asserting the key was *present* in the scaffold kept them
green the whole time.

This audit checks the other direction: for each declared key, is there a reader?

Readers are found by searching the source for the quoted dotted path, or a
quoted multi-segment ancestor of it. Comment lines are dropped at index time --
a comment naming a key is exactly what must not count.

Single-segment ancestors are excluded: a stage root like ``test`` appears quoted
all through the source as a stage name, which says nothing about ``test.enabled``.

Runtime-assembled paths (``f"quality.{language}.{tool}"``) are invisible to any
literal search, so they are declared in ``config/dynamic-config-keys.yaml``
against the file that reads them.

Usage:
    python3 scripts/audit-config-keys.py           # report + exit 1 on failure
    python3 scripts/audit-config-keys.py --list    # every key and its verdict
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "hyperi_ci"
DEFAULTS = SRC / "config" / "defaults.yaml"
ALLOWLIST = ROOT / "config" / "dynamic-config-keys.yaml"


def _index(roots: list[Path], suffixes: set[str]) -> str:
    """Every non-comment source line under roots, joined into one haystack."""
    kept: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in suffixes or not path.is_file():
                continue
            if path == DEFAULTS:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            kept += [
                line for line in text.splitlines() if not line.strip().startswith("#")
            ]
    return "\n".join(kept)


def _leaf_keys(node: object, trail: list[str]) -> list[str]:
    """Every leaf path in a nested mapping, dotted."""
    out: list[str] = []
    if isinstance(node, dict) and node:
        for key, value in node.items():
            out += _leaf_keys(value, [*trail, str(key)])
    elif trail:
        out.append(".".join(trail))
    return out


def _quoted(path: str, haystack: str) -> bool:
    """True when the dotted path appears quoted, or as a quoted prefix."""
    return re.search(rf"""["']{re.escape(path)}["'.]""", haystack) is not None


def _has_reader(path: str, haystack: str) -> bool:
    """True when the key, or a multi-segment ancestor, is read from source."""
    parts = path.split(".")
    for cut in range(len(parts), 1, -1):
        if _quoted(".".join(parts[:cut]), haystack):
            return True
    return len(parts) == 1 and _quoted(path, haystack)


def _load_allowlist() -> dict[str, str]:
    if not ALLOWLIST.is_file():
        return {}
    data = yaml.safe_load(ALLOWLIST.read_text(encoding="utf-8")) or {}
    return dict(data.get("keys") or {})


def audit() -> tuple[list[str], list[str], list[str]]:
    """Audit the declared keys.

    Returns:
        ``(unread, stale_allowlist, unused_allowlist)`` — keys nothing reads,
        allowlist entries naming a file that no longer exists, and allowlist
        entries the audit would have passed anyway.

    """
    haystack = (
        _index([SRC], {".py"}) + "\n" + _index([ROOT / ".github"], {".yml", ".yaml"})
    )
    declared = _leaf_keys(
        yaml.safe_load(DEFAULTS.read_text(encoding="utf-8")) or {}, []
    )
    allowed = _load_allowlist()

    found = {key: _has_reader(key, haystack) for key in declared}
    unread = [key for key, ok in found.items() if not ok and key not in allowed]
    stale = [
        f"{key} -> {reader}"
        for key, reader in allowed.items()
        if not (ROOT / reader).is_file()
    ]
    unused = [key for key in allowed if found.get(key)]
    return unread, stale, unused


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="print every key and its verdict"
    )
    args = parser.parse_args()

    unread, stale, unused = audit()

    if args.list:
        haystack = (
            _index([SRC], {".py"})
            + "\n"
            + _index([ROOT / ".github"], {".yml", ".yaml"})
        )
        allowed = _load_allowlist()
        for key in _leaf_keys(
            yaml.safe_load(DEFAULTS.read_text(encoding="utf-8")) or {}, []
        ):
            if _has_reader(key, haystack):
                verdict = "read"
            elif key in allowed:
                verdict = f"dynamic ({allowed[key]})"
            else:
                verdict = "UNREAD"
            print(f"{verdict:<48} {key}")
        print()

    if unread:
        print("Declared in defaults.yaml, read by nothing:")
        for key in unread:
            print(f"  {key}")
        print(
            "\nEither delete the key (and whatever scaffolds it), or -- if it is\n"
            "read by a runtime-assembled path -- declare it in\n"
            "config/dynamic-config-keys.yaml against the file that reads it."
        )
    if stale:
        print("\nAllowlist entries whose reader no longer exists:")
        for entry in stale:
            print(f"  {entry}")
    if unused:
        print("\nAllowlist entries the audit finds anyway (drop them):")
        for key in unused:
            print(f"  {key}")

    if unread or stale or unused:
        return 1
    print("Every key declared in defaults.yaml has a reader.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
