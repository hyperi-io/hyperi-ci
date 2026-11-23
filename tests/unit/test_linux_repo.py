# Project:   HyperI CI
# File:      tests/unit/test_linux_repo.py
# Purpose:   apt, rpm and apk repository helpers that need no repo tools
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from hyperi_ci.linux_repo import (
    Package,
    Target,
    apk_signature_segment,
    apt_suite,
    parse_apkindex,
    prune,
    publish,
)

TARGET = Target("app", "release", 10, "app-release", "kv/ci/app/package-signing")


@pytest.mark.parametrize(
    ("channel", "suite"),
    [("release", "stable"), ("beta", "beta"), ("alpha", "alpha")],
)
def test_apt_suite(channel: str, suite: str) -> None:
    assert apt_suite(channel) == suite


def test_parse_apkindex_reads_each_entry() -> None:
    text = "C:Q1abc=\nP:app\nV:1.2.0-r0\nA:x86_64\n\nP:app\nV:1.3.0-r0\nA:aarch64\n"

    assert parse_apkindex(text) == [
        {"C": "Q1abc=", "P": "app", "V": "1.2.0-r0", "A": "x86_64"},
        {"P": "app", "V": "1.3.0-r0", "A": "aarch64"},
    ]


@pytest.mark.parametrize("text", ["", "\n\n"])
def test_parse_apkindex_of_nothing(text: str) -> None:
    assert parse_apkindex(text) == []


def _numeric(a: str, b: str) -> int:
    key_a, key_b = [int(x) for x in a.split(".")], [int(x) for x in b.split(".")]
    return (key_a > key_b) - (key_a < key_b)


def _pkgs(*specs: tuple[str, str, str]) -> list[Package]:
    return [Package(Path(f"{n}-{v}.{a}"), n, v, a) for n, v, a in specs]


@pytest.mark.parametrize(
    ("keep", "stale"),
    [
        (2, {"app-1.2.0.amd64", "app-1.9.0.amd64"}),
        (3, {"app-1.2.0.amd64"}),
        (4, set()),
        (0, set()),
        (-1, set()),
    ],
)
def test_prune_keeps_the_newest_per_name_and_arch(keep: int, stale: set[str]) -> None:
    packages = _pkgs(
        ("app", "1.10.0", "amd64"),
        ("app", "1.2.0", "amd64"),
        ("app", "1.9.0", "amd64"),
        ("app", "1.11.0", "amd64"),
        ("app", "1.2.0", "arm64"),
        ("tool", "0.1.0", "amd64"),
    )

    assert {p.path.name for p in prune(packages, keep, _numeric)} == stale


def test_signature_segment_is_one_member_with_no_end_of_archive() -> None:
    signature = b"\x01" * 256
    segment = gzip.decompress(apk_signature_segment(signature, "app.rsa.pub"))

    # One 512-byte header, then the signature padded to a block, nothing after.
    assert len(segment) == 1024
    with tarfile.open(fileobj=io.BytesIO(segment + b"\0" * 1024)) as tar:
        (member,) = tar.getmembers()
        assert member.name == ".SIGN.RSA.app.rsa.pub"
        assert tar.extractfile(member).read() == signature


def test_signature_segment_pads_a_short_signature() -> None:
    segment = gzip.decompress(apk_signature_segment(b"sig", "k.rsa.pub"))

    assert len(segment) == 1024
    assert segment[512:515] == b"sig"
    assert segment[515:] == b"\0" * 509


def test_publish_with_no_packages_fails(tmp_path: Path) -> None:
    (tmp_path / "app.tar.gz").write_bytes(b"not a package")

    assert publish(tmp_path, TARGET) == 1


@pytest.mark.parametrize("missing", ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"])
def test_publish_without_r2_credentials_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "app_amd64.deb").write_bytes(b"deb")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv(missing)

    assert publish(tmp_path, TARGET) == 1
