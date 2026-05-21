# Project:   HyperI CI
# File:      src/hyperi_ci/quality/ignores.py
# Purpose:   Generic quality ignore-list parser
#
# License:   Proprietary -- HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Generic quality ignore-list, consumed by every language runner.

Schema in ``.hyperi-ci.yaml``::

    quality:
      ignore:
        - tool: pip-audit                # exact tool slug
          id: PYSEC-2025-183             # native to the tool's ID space
          reason: "Disputed; key length is application responsibility"
        - tool: cargo-audit
          id: RUSTSEC-2020-0070
          reason: "Transitive via reqwest-retry; tracking upstream issue #123"

The shape is identical across languages. Each language quality runner
filters for the slugs it owns and translates ``id`` to the tool's
native ignore flag at command-build time. ``reason`` is mandatory --
ignores are debt and a grep-able rationale is the price of admission.

Tool slugs in use:

* Python:   ``pip-audit``, ``semgrep``, ``bandit``, ``ruff``
* Rust:     ``cargo-audit``
* Go:       ``govulncheck``, ``golangci-lint``
* TypeScript: ``pnpm-audit`` (``npm audit`` has no CLI ignore flag;
  use ``package.json`` ``overrides`` instead)

Unknown tool slugs are accepted at load-time but silently ignored at
runtime -- this lets projects pre-stage entries for tools that
haven't been wired yet without breaking the build.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IgnoreEntry:
    """One ignored finding, scoped to a single quality tool."""

    tool: str
    id: str
    reason: str


def load_ignores(config_raw: dict[str, Any]) -> list[IgnoreEntry]:
    """Parse ``quality.ignore`` from the raw ``.hyperi-ci.yaml`` dict.

    Returns:
        List of ignore entries (empty if absent).

    Raises:
        ValueError: If the section is malformed or any entry is missing
            ``tool``, ``id``, or ``reason``.

    """
    quality = config_raw.get("quality") or {}
    raw_list = quality.get("ignore") or []
    if not isinstance(raw_list, list):
        raise ValueError(
            f"quality.ignore must be a list (got {type(raw_list).__name__})"
        )

    entries: list[IgnoreEntry] = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            raise ValueError(f"quality.ignore[{i}] must be a mapping")
        missing = [k for k in ("tool", "id", "reason") if not item.get(k)]
        if missing:
            raise ValueError(
                f"quality.ignore[{i}] missing required field(s): {', '.join(missing)}"
            )
        entries.append(
            IgnoreEntry(
                tool=str(item["tool"]).strip(),
                id=str(item["id"]).strip(),
                reason=str(item["reason"]).strip(),
            )
        )
    return entries


def for_tool(entries: Iterable[IgnoreEntry], tool: str) -> list[IgnoreEntry]:
    """Return entries scoped to a given tool slug."""
    return [e for e in entries if e.tool == tool]
