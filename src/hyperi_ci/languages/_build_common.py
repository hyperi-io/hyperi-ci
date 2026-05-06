# Project:   HyperI CI
# File:      src/hyperi_ci/languages/_build_common.py
# Purpose:   Shared helpers used by per-language build modules
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared utilities for per-language build modules.

These helpers are independent of language toolchains (cargo, go, npm,
uv) but are needed by all of them. Lifted out of the language-specific
build modules to remove copy-paste duplication and ensure they evolve
in lockstep.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from hyperi_ci.common import info


def human_size(size: int) -> str:
    """Convert bytes to human-readable size (e.g. 1024 → "1K").

    Public alias used to be ``_human_size`` in rust/build.py and
    golang/build.py. Identical behaviour.
    """
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size}{unit}"
        size //= 1024
    return f"{size}T"


def generate_checksums(output_dir: Path) -> None:
    """Generate ``checksums.sha256`` for every binary in ``output_dir``.

    Written in the format ``sha256sum -c`` expects::

        <sha256>  <filename>

    Excludes the checksums file itself. No-op when output_dir contains
    no files (silent — same behaviour as the per-language copies it
    replaces).
    """
    checksum_file = output_dir / "checksums.sha256"
    lines: list[str] = []

    for f in sorted(output_dir.iterdir()):
        if f.is_file() and f.name != "checksums.sha256":
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            lines.append(f"{sha}  {f.name}")

    if lines:
        checksum_file.write_text("\n".join(lines) + "\n")
        info(f"Checksums written to {checksum_file}")
