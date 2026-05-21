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
    """Write a per-binary ``{binary}.sha256`` file next to each artefact.

    Each output file gets its own sibling ``.sha256`` in the format
    ``sha256sum -c`` expects::

        <sha256>  <filename>

    Per-binary filenames (rather than one aggregated ``checksums.sha256``)
    let multi-arch matrix builds upload to the same R2 path without
    last-write-wins: ``macbash-linux-amd64.sha256`` and
    ``macbash-linux-arm64.sha256`` never collide. Downstream consumers
    that need a combined file can concatenate the per-arch ones.

    Excludes existing ``.sha256`` siblings so the call is idempotent.
    No-op when ``output_dir`` contains no files.
    """
    count = 0
    for f in sorted(output_dir.iterdir()):
        if not f.is_file() or f.suffix == ".sha256":
            continue
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        sha_path = f.with_name(f.name + ".sha256")
        sha_path.write_text(f"{sha}  {f.name}\n")
        info(f"Wrote {sha_path.name}")
        count += 1
    if count:
        info(f"Per-binary checksums written ({count} file(s)) to {output_dir}/")
