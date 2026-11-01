# Project:   HyperI CI
# File:      tests/unit/test_static_libs.py
# Purpose:   Tests for the static-libs prefix builder
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from hyperi_ci import static_libs as sl
from hyperi_ci import versions


def _tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestPins:
    def test_shipped_pins_are_complete_and_in_build_order(self) -> None:
        pins = versions.static_libs()
        assert list(pins) == ["openssl", "zlib", "zstd", "librdkafka"]
        for name, pin in pins.items():
            assert len(pin["sha256"]) == 64, name
            assert pin["url"].startswith("https://"), name
            assert "{version}" in pin["url"], name
        assert pins["openssl"]["version"].startswith(pins["openssl"]["lts"] + ".")

    @pytest.mark.parametrize(
        ("section", "missing"),
        [
            pytest.param(None, "`static_libs` is missing", id="no-section"),
            pytest.param(
                {"zlib": {"version": "1", "url": "u"}}, "zlib.sha256", id="no-sha"
            ),
            pytest.param({"zlib": "1.3"}, "zlib.version", id="not-a-mapping"),
        ],
    )
    def test_a_broken_section_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, section: object, missing: str
    ) -> None:
        monkeypatch.setattr(versions, "_data", lambda: {"static_libs": section})
        with pytest.raises(KeyError, match=missing):
            versions.static_libs()


class TestEveryPinHasARecipe:
    @pytest.mark.parametrize("name", list(versions.static_libs()))
    def test_recipe(self, name: str, tmp_path: Path) -> None:
        steps = sl._recipe(name, tmp_path, 4)
        assert steps
        assert all(step.argv for step in steps)

    def test_librdkafka_leaves_out_gssapi_and_curl_and_downloads_nothing(
        self, tmp_path: Path
    ) -> None:
        configure = sl._recipe("librdkafka", tmp_path, 4)[0].argv
        for flag in (
            "--disable-gssapi",
            "--disable-curl",
            "--no-download",
            "--enable-ssl",
        ):
            assert flag in configure

    def test_openssl_reads_the_system_ca_store(self, tmp_path: Path) -> None:
        assert "--openssldir=/etc/ssl" in sl._recipe("openssl", tmp_path, 4)[0].argv

    def test_an_unknown_library_has_no_recipe(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError, match="static_libs.lz4"):
            sl._recipe("lz4", tmp_path, 4)


def _prefix(
    tmp_path: Path, *, archives: tuple[str, ...], rdkafka_pc: str | None
) -> Path:
    prefix = tmp_path / "prefix"
    (prefix / "lib" / "pkgconfig").mkdir(parents=True)
    for name in archives:
        (prefix / "lib" / name).write_bytes(b"!<arch>\n")
    if rdkafka_pc is not None:
        (prefix / "lib" / "pkgconfig" / "rdkafka.pc").write_text(rdkafka_pc)
    return prefix


class TestCheck:
    @pytest.mark.parametrize(
        ("archives", "pc", "problems"),
        [
            pytest.param(sl._ARCHIVES, "Libs.private: -lssl -lzstd", [], id="complete"),
            pytest.param(
                sl._ARCHIVES[:-1],
                "Libs: -lrdkafka",
                ["missing lib/librdkafka.a"],
                id="no-rdkafka-a",
            ),
            pytest.param(
                sl._ARCHIVES, None, ["missing lib/pkgconfig/rdkafka.pc"], id="no-pc"
            ),
            pytest.param(
                sl._ARCHIVES,
                "Libs.private: -lsasl2 -lcurl",
                ["rdkafka.pc links sasl2", "rdkafka.pc links curl"],
                id="gssapi-and-curl-leaked",
            ),
        ],
    )
    def test_check(
        self,
        tmp_path: Path,
        archives: tuple[str, ...],
        pc: str | None,
        problems: list[str],
    ) -> None:
        assert (
            sl._check(_prefix(tmp_path, archives=archives, rdkafka_pc=pc)) == problems
        )


class TestRelocatable:
    def test_every_pc_names_its_prefix_relative_to_itself(self, tmp_path: Path) -> None:
        prefix = _prefix(tmp_path, archives=(), rdkafka_pc=None)
        pc = prefix / "lib" / "pkgconfig" / "rdkafka.pc"
        pc.write_text(
            f"prefix={prefix}\nlibdir={prefix}/lib\nLibs.private: -L{prefix}/lib -lssl\n"
        )
        sl._make_relocatable(prefix)
        assert pc.read_text() == (
            "prefix=${pcfiledir}/../..\nlibdir=${prefix}/lib\n"
            "Libs.private: -L${prefix}/lib -lssl\n"
        )

    def test_prune_keeps_only_archives_headers_and_pc_files(
        self, tmp_path: Path
    ) -> None:
        prefix = _prefix(tmp_path, archives=("libz.a",), rdkafka_pc="x")
        (prefix / "lib" / "librdkafka.so.1").write_bytes(b"elf")
        (prefix / "include").mkdir()
        for unwanted in ("bin", "share"):
            (prefix / unwanted).mkdir()
        sl._prune(prefix)
        assert sorted(p.name for p in prefix.iterdir()) == ["include", "lib"]
        assert not list((prefix / "lib").glob("*.so*"))


class TestFetchSource:
    @pytest.fixture
    def served(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
        body: dict[str, bytes] = {}

        def fake_fetch(
            url: str, dest: Path, **_: object
        ) -> subprocess.CompletedProcess[str]:
            dest.write_bytes(body["data"])
            return subprocess.CompletedProcess([url], 0, "200", "")

        monkeypatch.setattr(sl, "curl_fetch", fake_fetch)
        return body

    @staticmethod
    def _pin(data: bytes, digest: str | None = None) -> dict[str, str]:
        return {
            "version": "1.0",
            "url": "https://example.test/x-{version}.tar.gz",
            "sha256": digest or hashlib.sha256(data).hexdigest(),
        }

    def test_a_good_archive_unpacks_to_its_top_directory(
        self, served: dict[str, bytes], tmp_path: Path
    ) -> None:
        served["data"] = _tarball({"x-1.0/configure": b"#!/bin/sh\n"})
        top = sl._fetch_source("x", self._pin(served["data"]), tmp_path)
        assert top == tmp_path / "x" / "x-1.0"
        assert (top / "configure").is_file()

    @pytest.mark.parametrize(
        ("members", "wrong_digest"),
        [
            pytest.param({"x-1.0/configure": b"a"}, True, id="digest-mismatch"),
            pytest.param(
                {"a/configure": b"a", "b/configure": b"b"}, False, id="two-tops"
            ),
        ],
    )
    def test_a_bad_archive_is_refused(
        self,
        served: dict[str, bytes],
        tmp_path: Path,
        members: dict[str, bytes],
        wrong_digest: bool,
    ) -> None:
        served["data"] = _tarball(members)
        pin = self._pin(served["data"], "0" * 64 if wrong_digest else None)
        assert sl._fetch_source("x", pin, tmp_path) is None


class TestReleaseTag:
    def test_the_tag_names_the_versions_and_is_stable(self) -> None:
        tag = sl.release_tag()
        pins = versions.static_libs()
        assert tag.startswith(
            f"static-libs-rdkafka{pins['librdkafka']['version']}-"
            f"openssl{pins['openssl']['version']}-"
        )
        assert tag == sl.release_tag()
        assert not tag.startswith("v")

    def test_a_changed_recipe_gets_a_new_tag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = sl.release_tag()
        real = sl._recipe

        def one_more_flag(name: str, prefix: Path, jobs: int) -> list[sl._Step]:
            steps = real(name, prefix, jobs)
            if name == "zlib":
                steps[0] = sl._Step((*steps[0].argv, "--zlib-compat"))
            return steps

        monkeypatch.setattr(sl, "_recipe", one_more_flag)
        assert sl.release_tag() != before


class TestPublish:
    @staticmethod
    def _out(tmp_path: Path, triples: tuple[str, ...]) -> Path:
        out = tmp_path / "out"
        out.mkdir()
        for triple in triples:
            for suffix in (".tar.gz", ".tar.gz.sha256", ".json"):
                (out / f"hyperi-static-libs-{triple}{suffix}").write_text("x")
        return out

    def test_an_existing_release_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: list[list[str]] = []

        def fake_gh(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            seen.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(sl, "gh_run", fake_gh)
        assert (
            sl.publish(self._out(tmp_path, tuple(sl._TRIPLES.values())), target="abc")
            == 0
        )
        assert [args[:2] for args in seen] == [["release", "view"]]

    @pytest.mark.parametrize(
        ("triples", "rc"),
        [
            pytest.param(tuple(sl._TRIPLES.values()), 0, id="both-arches"),
            pytest.param(("x86_64-unknown-linux-gnu",), 1, id="arm64-missing"),
        ],
    )
    def test_a_new_release_needs_every_arch_and_is_never_latest(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        triples: tuple[str, ...],
        rc: int,
    ) -> None:
        seen: list[list[str]] = []

        def fake_gh(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            seen.append(args)
            missing = args[:2] == ["release", "view"]
            return subprocess.CompletedProcess(args, 1 if missing else 0, "", "")

        monkeypatch.setattr(sl, "gh_run", fake_gh)
        assert sl.publish(self._out(tmp_path, triples), target="abc") == rc
        creates = [args for args in seen if args[:2] == ["release", "create"]]
        if rc:
            assert not creates
            return
        (create,) = creates
        assert "--latest=false" in create
        assert create[create.index("--target") + 1] == "abc"
        assert sum(a.endswith(".tar.gz") for a in create) == len(sl._TRIPLES)


class TestBuild:
    def test_an_existing_prefix_is_refused(self, tmp_path: Path) -> None:
        prefix = tmp_path / "prefix"
        prefix.mkdir()
        assert sl.build(prefix, tmp_path / "work") == 1

    def test_package_writes_the_archive_and_its_digest(self, tmp_path: Path) -> None:
        prefix = _prefix(tmp_path, archives=("libz.a",), rdkafka_pc="x")
        (prefix / sl.MANIFEST).write_text('{"target": "x86_64-unknown-linux-gnu"}')
        archive = sl.package(prefix, tmp_path / "out")
        assert archive.name == "hyperi-static-libs-x86_64-unknown-linux-gnu.tar.gz"
        assert (
            tmp_path / "out" / "hyperi-static-libs-x86_64-unknown-linux-gnu.json"
        ).is_file()
        digest, name = archive.with_name(archive.name + ".sha256").read_text().split()
        assert name == archive.name
        assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
        with tarfile.open(archive) as tar:
            assert "hyperi-static-libs/lib/libz.a" in tar.getnames()
