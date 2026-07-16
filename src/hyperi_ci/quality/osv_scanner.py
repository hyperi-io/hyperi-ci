# Project:   HyperI CI
# File:      src/hyperi_ci/quality/osv_scanner.py
# Purpose:   Malicious-package scanning via osv-scanner (Rust + TS gap)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""osv-scanner helper: the malicious-package (``MAL-*``) detection layer.

cargo-audit (RustSec) and npm/pnpm audit (GitHub Advisory DB) cover
*known vulnerabilities* but NOT the OSSF malicious-packages feed.
osv-scanner reads that feed (the one ossf/malicious-packages amends),
so it closes the typosquat / compromised-maintainer gap for Rust and
TypeScript. Python is already covered (pip-audit queries OSV directly).

It is defence-in-depth behind the Renovate 7-day cooldown, so it runs
at ``warn`` by default: the same OSV feed periodically ships false-
positive waves (see ossf/malicious-packages#1276), and a blocking gate
on a feed that misfires would red the build on legitimate packages.
True positives are acted on; known false positives are suppressed via
``quality.ignore`` (which generates this scanner's native
``[[IgnoredVulns]]`` config, with optional auto-expiry).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from hyperi_ci.common import info
from hyperi_ci.quality.ignores import IgnoreEntry

SLUG = "osv-scanner"
_BINARY = "osv-scanner"
_CONFIG_NAME = "osv-scanner.toml"


def available() -> bool:
    """Return True if the osv-scanner binary is on PATH."""
    return shutil.which(_BINARY) is not None


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_ignore_config(entries: Iterable[IgnoreEntry]) -> str:
    """Render an ``osv-scanner.toml`` body from ignore entries.

    Each entry becomes an ``[[IgnoredVulns]]`` block. ``expires`` maps
    to osv-scanner's native ``ignoreUntil`` (RFC3339), so a suppression
    self-clears once the date passes - belt-and-braces with the
    framework-level drop in ``load_ignores``.

    Returns:
        TOML text (empty string when there are no entries).

    """
    blocks: list[str] = []
    for e in entries:
        lines = ["[[IgnoredVulns]]", f'id = "{_toml_escape(e.id)}"']
        if e.expires is not None:
            lines.append(f"ignoreUntil = {e.expires.isoformat()}T00:00:00Z")
        lines.append(f'reason = "{_toml_escape(e.reason)}"')
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def build_command(lockfile: Path, config_path: Path | None = None) -> list[str]:
    """Compose the osv-scanner CLI invocation for a single lockfile.

    osv-scanner v2 scans a named lockfile via ``scan source --lockfile``;
    ``--config`` points at the generated ignore config.
    """
    cmd = [_BINARY, "scan", "source", "--lockfile", str(lockfile)]
    if config_path is not None:
        cmd += ["--config", str(config_path)]
    return cmd


def run(
    lockfile: Path,
    entries: Iterable[IgnoreEntry],
    mode: str,
    run_tool: Callable[[str, list[str], str], bool],
    *,
    write_dir: Path | None = None,
) -> bool:
    """Run osv-scanner against ``lockfile``, delegating execution.

    Auto-detects the binary and skips with a notice if it is absent
    (same pattern as every other optional tool). When ignore entries
    are present, writes an ``osv-scanner.toml`` and points the scanner
    at it. Execution + blocking/warn/disabled semantics are delegated
    to ``run_tool`` (the caller's tool runner), so this stays uniform
    with the rest of the quality stage.

    Returns:
        True on pass / skip; ``run_tool``'s result otherwise.

    """
    if mode == "disabled":
        info(f"  {SLUG}: disabled")
        return True

    if not available():
        info(
            f"  {SLUG}: binary not found — skipping malicious-package scan "
            f"of {lockfile.name} (install osv-scanner to enable)"
        )
        return True

    if not lockfile.exists():
        info(f"  {SLUG}: no {lockfile.name} found — skipping")
        return True

    entries = list(entries)
    config_path: Path | None = None
    if entries:
        target_dir = write_dir or lockfile.resolve().parent
        config_path = target_dir / _CONFIG_NAME
        config_path.write_text(render_ignore_config(entries))

    return run_tool(SLUG, build_command(lockfile, config_path), mode)
