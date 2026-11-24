# Project:   HyperI CI
# File:      tests/unit/test_linux_repo.py
# Purpose:   apt, rpm and apk repository helpers that need no repo tools
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from hyperi_ci.linux_repo import (
    Package,
    RepoError,
    Target,
    apk_signature_segment,
    apt_suite,
    check_sums,
    parse_apkindex,
    prune,
    publish,
    signing_key,
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
        extracted = tar.extractfile(member)
        assert extracted is not None
        assert extracted.read() == signature


def test_signature_segment_pads_a_short_signature() -> None:
    segment = gzip.decompress(apk_signature_segment(b"sig", "k.rsa.pub"))

    assert len(segment) == 1024
    assert segment[512:515] == b"sig"
    assert segment[515:] == b"\0" * 509


PRIMARY = "sec:u:255:22:AAAA:1:::u:::scSC:::{serial}:::ed25519:::0:\nfpr:::::::::PRIMARYFPR:\n"
SIGN_SUB = "ssb:u:255:22:BBBB:1::::::s:::+:::ed25519::\nfpr:::::::::SUBFPR:\n"
ENC_SUB = "ssb:u:255:18:CCCC:1::::::e:::+:::cv25519::\nfpr:::::::::ENCFPR:\n"


@pytest.mark.parametrize(
    ("listing", "chosen"),
    [
        (PRIMARY.format(serial="+") + SIGN_SUB, "PRIMARYFPR!"),
        (PRIMARY.format(serial="#") + SIGN_SUB, "SUBFPR!"),
        (PRIMARY.format(serial="#") + ENC_SUB + SIGN_SUB, "SUBFPR!"),
        (PRIMARY.format(serial="#") + ENC_SUB, None),
        ("", None),
    ],
)
def test_signing_key_skips_a_stripped_primary(listing: str, chosen: str | None) -> None:
    assert signing_key(listing) == chosen


_TARBALL = b"agent tarball"
_DIGEST = hashlib.sha256(_TARBALL).hexdigest()


@pytest.mark.parametrize(
    ("listing", "checked", "fault"),
    [
        (f"{_DIGEST}  agent.tar.gz\n", 1, None),
        (f"{_DIGEST} *agent.tar.gz\n\n{_DIGEST}  sub/agent.tar.gz\n", 2, None),
        (f"{'0' * 64}  agent.tar.gz\n", 0, "does not match"),
        (f"{_DIGEST}  gone.tar.gz\n", 0, "does not hold"),
        (f"{_DIGEST}  ../agent.tar.gz\n", 0, "outside its directory"),
        (f"{_DIGEST}agent.tar.gz\n", 0, "line 1 is not"),
        (f"{_DIGEST.upper()}  agent.tar.gz\n", 0, "line 1 is not"),
        ("\n", 0, "lists no files"),
    ],
)
def test_check_sums(
    tmp_path: Path, listing: str, checked: int, fault: str | None
) -> None:
    root = tmp_path / "artifact"
    (root / "sub").mkdir(parents=True)
    for path in (root / "agent.tar.gz", root / "sub" / "agent.tar.gz"):
        path.write_bytes(_TARBALL)
    (tmp_path / "agent.tar.gz").write_bytes(_TARBALL)
    sums = root / "SHA256SUMS"
    sums.write_text(listing, encoding="utf-8")

    if fault is None:
        assert check_sums(sums) == checked
    else:
        with pytest.raises(RepoError, match=fault):
            check_sums(sums)


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
