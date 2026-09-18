# Project:   HyperI CI
# File:      src/hyperi_ci/languages/_build_common.py
# Purpose:   Shared helpers used by per-language build modules
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared utilities for per-language build modules.

These helpers are independent of language toolchains (cargo, go, npm,
uv) but are needed by all of them. Lifted out of the language-specific
build modules to remove copy-paste duplication and ensure they evolve
in lockstep.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import BinaryIO

from hyperi_ci.common import info

_ELF_MAGIC = b"\x7fELF"

# Section index escapes for a table too large for the 16-bit header fields:
# the real count and string-table index then live in section 0.
_SHN_XINDEX = 0xFFFF


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


def _elf_section_entry(
    handle: BinaryIO, offset: int, *, is_64: bool, order: str
) -> tuple[int, int, int, int]:
    """Read one section header as ``(name, offset, size, link)``."""
    layout = order + ("IIQQQQI" if is_64 else "IIIIIII")
    handle.seek(offset)
    raw = handle.read(struct.calcsize(layout))
    name, _, _, _, sh_offset, sh_size, sh_link = struct.unpack(layout, raw)
    return name, sh_offset, sh_size, sh_link


def elf_section_names(path: Path) -> set[str]:
    """Return the section names of an ELF file.

    Reads only the header, the section table and the section-name string
    table, so a large binary costs a few kilobytes of I/O and needs no
    binutils on the host.

    Args:
        path: File to inspect.

    Returns:
        The section names, or an empty set when the file is missing, not ELF,
        or truncated.

    """
    try:
        with path.open("rb") as handle:
            ident = handle.read(16)
            if len(ident) < 16 or ident[:4] != _ELF_MAGIC:
                return set()
            is_64 = ident[4] == 2
            order = "<" if ident[5] == 1 else ">"
            # Offsets below are relative to the end of e_ident.
            if is_64:
                header = handle.read(48)
                (shoff,) = struct.unpack_from(order + "Q", header, 24)
                shentsize, shnum, shstrndx = struct.unpack_from(
                    order + "HHH", header, 42
                )
            else:
                header = handle.read(36)
                (shoff,) = struct.unpack_from(order + "I", header, 16)
                shentsize, shnum, shstrndx = struct.unpack_from(
                    order + "HHH", header, 30
                )
            if not shoff or not shentsize:
                return set()

            def entry(index: int) -> tuple[int, int, int, int]:
                return _elf_section_entry(
                    handle, shoff + index * shentsize, is_64=is_64, order=order
                )

            if shnum == 0:
                shnum = entry(0)[2]
            if shstrndx == _SHN_XINDEX:
                shstrndx = entry(0)[3]

            _, strtab_offset, strtab_size, _ = entry(shstrndx)
            handle.seek(strtab_offset)
            strtab = handle.read(strtab_size)

            names: set[str] = set()
            for index in range(shnum):
                name_offset = entry(index)[0]
                end = strtab.find(b"\0", name_offset)
                name = strtab[name_offset : end if end >= 0 else None]
                if name:
                    names.add(name.decode("ascii", errors="replace"))
            return names
    except (OSError, struct.error):
        return set()
