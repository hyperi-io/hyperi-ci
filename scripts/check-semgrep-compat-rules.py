#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/check-semgrep-compat-rules.py
# Purpose:   Gate - the semgrep compatibility-rule table matches the registry pack
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Semgrep compatibility-rule drift check (issue #486).

`hyperi_ci.quality.semgrep` excludes the `python.lang.compatibility` rules a
project's `requires-python` floor has outgrown. `--exclude-rule` takes exact
ids only, so the table holds every id. A rule the registry renames or adds
then stops matching without a sound, and projects get the noise back.

The pack is served from the semgrep registry at scan time, so it can change
with no semgrep release at all. This fetches the pack the same way the CLI
does and compares its ids against the table.

Needs network, so it belongs on the scheduled audit rather than the PR path.

Usage:  uv run scripts/check-semgrep-compat-rules.py
Exit 1 on drift, 2 when the registry cannot be reached, 0 otherwise.
"""

import re
import sys
import urllib.request
from collections.abc import Callable

import yaml

from hyperi_ci.common import URL_ERRORS, url_read
from hyperi_ci.quality.semgrep import PYTHON_COMPAT_RULES

PACK_URL = "https://semgrep.dev/c/r/python.lang.compatibility"

# A rule id names its target version, e.g. `...python37.python37-compat...`.
_TARGET = re.compile(r"\.python(\d)(\d+)\.")


def fetch_pack() -> str | None:
    """Return the pack's YAML, or None when the registry cannot be reached.

    None is its own outcome. An unreachable registry and an empty pack would
    otherwise both read as "every rule was removed".
    """
    try:
        return url_read(urllib.request.Request(PACK_URL), timeout=30).decode(
            "utf-8", errors="replace"
        )
    except URL_ERRORS:
        return None


def pack_rule_ids(pack_yaml: str) -> set[str] | None:
    """Rule ids in a registry pack, or None when the YAML is not a rule pack."""
    try:
        data = yaml.safe_load(pack_yaml)
    except yaml.YAMLError:
        return None
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        return None
    return {r["id"] for r in rules if isinstance(r, dict) and "id" in r}


def target_version(rule_id: str) -> str | None:
    """Python version a compatibility rule id targets, e.g. "3.7"."""
    match = _TARGET.search(rule_id)
    return f"{match.group(1)}.{match.group(2)}" if match else None


def main(fetch: Callable[[], str | None] = fetch_pack) -> int:
    """Compare the registry pack against the table. Returns an exit code."""
    pack_yaml = fetch()
    if pack_yaml is None:
        print(f"Could not reach {PACK_URL} - table NOT checked.")
        return 2
    have = pack_rule_ids(pack_yaml)
    if not have:
        print(f"{PACK_URL} returned no rules - table NOT checked.")
        return 2

    want = set(PYTHON_COMPAT_RULES)
    gone = sorted(want - have)
    new = sorted(have - want)
    mislabelled = sorted(
        rid for rid in want & have if target_version(rid) != PYTHON_COMPAT_RULES[rid]
    )

    if not gone and not new and not mislabelled:
        print(f"Compatibility-rule table matches the registry ({len(want)} rules).")
        return 0

    if gone:
        print("In the table but NOT in the registry pack:")
        for rid in gone:
            print(f"  - {rid}")
        print("  Renamed or removed upstream, so the exclude no longer matches.")
    if new:
        print("In the registry pack but NOT in the table:")
        for rid in new:
            print(f"  - {rid} (targets {target_version(rid) or 'unknown'})")
        print("  Add it, or projects above its target get its findings.")
    if mislabelled:
        print("Table version disagrees with the id:")
        for rid in mislabelled:
            print(
                f"  - {rid}: table {PYTHON_COMPAT_RULES[rid]}, id {target_version(rid)}"
            )
    print("Fix PYTHON_COMPAT_RULES in src/hyperi_ci/quality/semgrep.py.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
