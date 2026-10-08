# Project:   HyperI CI
# File:      src/hyperi_ci/quality/doc_links.py
# Purpose:   Check repo-internal doc links and anchors with lychee
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Repo-internal link and anchor checking via lychee.

``--offline`` is always passed, so the result depends only on the commit:
external links fail for outages unrelated to it and are not checked here.
``--include-fragments`` resolves ``#heading`` anchors against the target's
headings. A repo silences a known-bad link with a committed ``.lycheeignore``.
"""

import functools
import json
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.native_tools import ci_binary
from hyperi_ci.quality import findings as fdg
from hyperi_ci.tools import missing_tool


@functools.cache
def _install_lychee() -> str | None:
    """Return lychee from PATH, else the pinned release installed on Linux CI.

    Cached so :func:`planned_mode` and :func:`run` share one install attempt.
    """
    return ci_binary("lychee")


def resolve_mode(config: CIConfig) -> str:
    """Resolve lychee's mode: ``warn`` (default) / ``blocking`` / ``disabled``."""
    return resolve_tool_mode("doc_links", config, default="warn")


def planned_mode(config: CIConfig) -> str | None:
    """Return the mode lychee will check links at, or None when it will not run.

    The orchestrator asks so :mod:`doc_paths` does not report the same link
    twice. It installs lychee so the answer matches what :func:`run` does.
    """
    mode = resolve_mode(config)
    if mode == "disabled":
        return None
    if _install_lychee() is None:
        return None
    return mode


def parse(stdout: str) -> list[fdg.Finding]:
    """Parse lychee ``--format json`` into normalised findings.

    Reads ``error_map`` only; ``excluded_map`` is the external links
    ``--offline`` skipped.
    """
    try:
        doc = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict):
        return []
    out: list[fdg.Finding] = []
    for path, entries in (doc.get("error_map") or {}).items():
        for entry in entries or []:
            status = entry.get("status") or {}
            span = entry.get("span") or {}
            out.append(
                fdg.Finding(
                    tool="doc-links",
                    path=str(path),
                    line=span.get("line"),
                    level="error",
                    rule="docs/broken-link",
                    message=f"{entry.get('url', '')} - {status.get('text', 'failed')}",
                )
            )
    return out


def run(
    files: list[Path],
    config: CIConfig,
    *,
    root: Path | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Check every internal link and anchor in ``files``; return the exit code.

    Returns 1 when a blocking check finds a broken link, or cannot run or
    complete in CI.
    """
    mode = resolve_mode(config)
    if mode == "disabled":
        info("  doc-links: disabled")
        return 0
    if not files:
        info("  doc-links: no markdown to check - skipping")
        return 0

    root = Path(root or Path.cwd())
    exe = _install_lychee()
    if not exe:
        return missing_tool("lychee", mode)

    info(f"  doc-links: checking {len(files)} markdown file(s)...")
    cmd = [
        exe,
        "--offline",
        "--include-fragments",
        "--no-progress",
        # lychee cannot resolve a `/README.md` link without an absolute root.
        "--root-dir",
        str(root.resolve()),
        "--format",
        "json",
        *[str(f) for f in files],
    ]
    # lychee exits 2 for broken links and for a rejected argument, so an exit 2
    # with no parsed link is the second.
    found = fdg.run_check(
        "doc-links", cmd, mode, lambda result: parse(result.stdout), cwd=root
    )
    if isinstance(found, int):
        return found

    fdg.report("doc-links", found, mode, sarif_path=sarif_path)

    if not found:
        success(f"  doc-links: every internal link in {len(files)} file(s) resolves")
        return 0
    if mode == "blocking":
        error(f"  doc-links: {len(found)} broken internal link(s) or anchor(s)")
        return 1
    warn(f"  doc-links: {len(found)} broken internal link(s) (non-blocking)")
    return 0
