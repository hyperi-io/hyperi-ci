#  Project:   HyperI CI
#  File:      tests/unit/test_build_common.py
#  Purpose:   Tests for languages/_build_common.py helpers
#  Language:  Python 3
#
#  License:   Proprietary — HYPERI PTY LIMITED
#  Copyright: (c) 2026 HYPERI PTY LIMITED
"""Unit tests for the shared build helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

from hyperi_ci.languages._build_common import generate_checksums, human_size


def test_human_size_units() -> None:
    assert human_size(0) == "0B"
    assert human_size(512) == "512B"
    assert human_size(2048) == "2K"
    assert human_size(5 * 1024 * 1024) == "5M"


def test_generate_checksums_writes_per_binary_files(tmp_path: Path) -> None:
    bin1 = tmp_path / "macbash-linux-amd64"
    bin1.write_bytes(b"binary one content")
    bin2 = tmp_path / "macbash-linux-arm64"
    bin2.write_bytes(b"binary two content")

    generate_checksums(tmp_path)

    sha1 = tmp_path / "macbash-linux-amd64.sha256"
    sha2 = tmp_path / "macbash-linux-arm64.sha256"
    assert sha1.is_file()
    assert sha2.is_file()

    expected_one = hashlib.sha256(b"binary one content").hexdigest()
    expected_two = hashlib.sha256(b"binary two content").hexdigest()
    assert sha1.read_text() == f"{expected_one}  macbash-linux-amd64\n"
    assert sha2.read_text() == f"{expected_two}  macbash-linux-arm64\n"


def test_generate_checksums_skips_existing_sha_files(tmp_path: Path) -> None:
    (tmp_path / "macbash-linux-amd64").write_bytes(b"a")
    (tmp_path / "stray.sha256").write_bytes(b"deadbeef  stray\n")

    generate_checksums(tmp_path)

    # We don't try to checksum a .sha256 file.
    assert not (tmp_path / "stray.sha256.sha256").exists()
    assert (tmp_path / "macbash-linux-amd64.sha256").is_file()


def test_generate_checksums_no_op_on_empty_dir(tmp_path: Path) -> None:
    generate_checksums(tmp_path)
    assert list(tmp_path.iterdir()) == []
