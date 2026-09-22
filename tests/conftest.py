# Project:   HyperI CI
# File:      tests/conftest.py
# Purpose:   Shared fixtures -- keep the suite off the developer's real config
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import struct
from collections.abc import Callable
from pathlib import Path

import pytest

from hyperi_ci import channel


def _write_elf(
    path: Path, sections: list[str], *, is_64: bool = True, little: bool = True
) -> Path:
    """Write a minimal valid ELF file whose section table carries ``sections``.

    Enough structure for a section-table reader -- header, name string table,
    section headers -- and nothing a loader would need.
    """
    order = "<" if little else ">"
    names = ["", *sections, ".shstrtab"]
    strtab = b""
    name_offsets: list[int] = []
    for name in names:
        name_offsets.append(len(strtab))
        strtab += name.encode("ascii") + b"\0"

    ehsize = 64 if is_64 else 52
    shentsize = 64 if is_64 else 40
    strtab_offset = ehsize
    shoff = strtab_offset + len(strtab)
    shnum = len(names)
    shstrndx = shnum - 1

    ident = b"\x7fELF" + bytes([2 if is_64 else 1, 1 if little else 2, 1]) + bytes(9)
    header_layout = order + ("HHIQQQIHHHHHH" if is_64 else "HHIIIIIHHHHHH")
    header = ident + struct.pack(
        header_layout,
        2,
        62,
        1,
        0,
        0,
        shoff,
        0,
        ehsize,
        0,
        0,
        shentsize,
        shnum,
        shstrndx,
    )

    section_layout = order + ("IIQQQQIIQQ" if is_64 else "IIIIIIIIII")
    table = b""
    for index, name_offset in enumerate(name_offsets):
        is_strtab = index == shstrndx
        sh_type = 3 if is_strtab else (0 if index == 0 else 7)
        table += struct.pack(
            section_layout,
            name_offset,
            sh_type,
            0,
            0,
            strtab_offset if is_strtab else 0,
            len(strtab) if is_strtab else 0,
            0,
            0,
            1,
            0,
        )

    path.write_bytes(header + strtab + table)
    return path


@pytest.fixture
def make_elf() -> Callable[..., Path]:
    """Factory for real ELF files with a chosen section table."""
    return _write_elf


@pytest.fixture(autouse=True)
def isolated_channel_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point auto-update channel/freeze state at tmp_path for every test.

    Without this a developer who has run `hyperi-ci autoupdate freeze` (or has
    hyperi-ai frozen) sees auto-update tests fail on their machine and pass in
    CI. The state is two files in the homedir, so the only safe default is to
    redirect both tools' directories.

    Returns:
        The redirected hyperi-ci config directory.

    """
    ci_dir = tmp_path / "config-hyperi-ci"
    ai_dir = tmp_path / "config-hyperi-ai"
    monkeypatch.setattr(channel, "CONFIG_DIR", ci_dir)
    monkeypatch.setattr(channel, "AI_CONFIG_DIR", ai_dir)
    return ci_dir
