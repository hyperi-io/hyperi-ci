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
        - tool: osv-scanner              # batch form for a whole FP wave
          ids: [MAL-2026-4228, MAL-2026-4359]
          reason: "ossf/malicious-packages#1276 false-positive withdrawal"
          expires: 2026-06-15            # auto-sunsets; dropped after this date

The shape is identical across languages. Each language quality runner
filters for the slugs it owns and translates ``id`` to the tool's
native ignore flag at command-build time. ``reason`` is mandatory --
ignores are debt and a grep-able rationale is the price of admission.

``id`` (scalar) and ``ids`` (list) may both be given; they merge. The
list form keeps one stanza per logical suppression (e.g. an entire
malicious-packages false-positive wave) instead of N near-identical
entries.

``expires`` (optional ``YYYY-MM-DD``) sunsets an ignore: once the date
has passed the entry is dropped at load time and a warning is logged,
so a suppression for a withdrawn false positive cannot silently mask a
genuine future finding on the same ID. This is framework-wide -- every
language runner inherits it because filtering happens here, not in the
per-tool translation.

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
from datetime import date
from typing import Any

from hyperi_ci.common import warn


@dataclass(frozen=True)
class IgnoreEntry:
    """One ignored finding, scoped to a single quality tool."""

    tool: str
    id: str
    reason: str
    expires: date | None = None


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
    today = date.today()
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            raise ValueError(f"quality.ignore[{i}] must be a mapping")

        missing = [k for k in ("tool", "reason") if not item.get(k)]
        if missing:
            raise ValueError(
                f"quality.ignore[{i}] missing required field(s): {', '.join(missing)}"
            )

        ids: list[str] = []
        if item.get("id"):
            ids.append(str(item["id"]).strip())
        raw_ids = item.get("ids")
        if raw_ids is not None:
            if not isinstance(raw_ids, list):
                raise ValueError(
                    f"quality.ignore[{i}] 'ids' must be a list "
                    f"(got {type(raw_ids).__name__})"
                )
            ids.extend(s for x in raw_ids if (s := str(x).strip()))
        if not ids:
            raise ValueError(f"quality.ignore[{i}] requires 'id' or 'ids'")

        expires: date | None = None
        raw_expires = item.get("expires")
        if raw_expires is not None and str(raw_expires).strip():
            try:
                expires = date.fromisoformat(str(raw_expires).strip())
            except ValueError as e:
                raise ValueError(
                    f"quality.ignore[{i}] 'expires' must be an ISO date "
                    f"(YYYY-MM-DD), got {raw_expires!r}"
                ) from e

        tool = str(item["tool"]).strip()
        reason = str(item["reason"]).strip()

        if expires is not None and expires < today:
            for _id in ids:
                warn(
                    f"quality.ignore lapsed: {tool} {_id} expired "
                    f"{expires.isoformat()} — re-evaluate or remove the entry"
                )
            continue

        for _id in ids:
            entries.append(
                IgnoreEntry(tool=tool, id=_id, reason=reason, expires=expires)
            )
    return entries


def for_tool(entries: Iterable[IgnoreEntry], tool: str) -> list[IgnoreEntry]:
    """Return entries scoped to a given tool slug."""
    return [e for e in entries if e.tool == tool]
