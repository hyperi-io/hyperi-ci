"""Licence text from a package's repository, else the SPDX standard text."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from hyperi_ci.quality import upstream_licences as up
from hyperi_ci.quality.dep_licenses import (
    Finding,
    Mode,
    NoticeEntry,
    Verdict,
    _with_upstream_texts,
)


@pytest.mark.parametrize(
    ("repository", "expected"),
    [
        (
            "https://github.com/cel-rust/cel-rust",
            ("https://github.com/cel-rust/cel-rust", ""),
        ),
        (
            "https://github.com/boinkor-net/governor.git",
            ("https://github.com/boinkor-net/governor", ""),
        ),
        (
            "https://github.com/open-telemetry/opentelemetry-rust/tree/main/opentelemetry-sdk",
            (
                "https://github.com/open-telemetry/opentelemetry-rust",
                "opentelemetry-sdk",
            ),
        ),
        (
            "https://gitlab.com/g/p/-/tree/main/crates/x",
            ("https://gitlab.com/g/p", "crates/x"),
        ),
        ("https://git.sr.ht/~u/repo", ("https://git.sr.ht/~u/repo", "")),
        ("http://github.com/o/r", None),
        ("git@github.com:o/r.git", None),
        ("", None),
        (None, None),
    ],
)
def test_repository_source(repository, expected):
    assert up.repository_source(repository) == expected


def test_tag_candidates_cover_the_usual_release_tags():
    tags = up.tag_candidates("opentelemetry_sdk", "0.31.0")
    assert tags[:2] == ["v0.31.0", "0.31.0"]
    assert "opentelemetry-sdk-0.31.0" in tags
    assert "opentelemetry_sdk-v0.31.0" in tags


@pytest.mark.parametrize(
    ("tags", "name", "version", "picked"),
    [
        ({"v0.14.5", "cel-v0.11.5"}, "cel", "0.14.5", "v0.14.5"),
        ({"opentelemetry-0.31.0"}, "opentelemetry", "0.31.0", "opentelemetry-0.31.0"),
        ({"v4.10.0"}, "rdkafka-sys", "4.10.0+2.12.1", "v4.10.0"),
        (
            {"v0.15.3", "clickhouse-types/v0.1.4"},
            "clickhouse-types",
            "0.1.4",
            "clickhouse-types/v0.1.4",
        ),
        ({"v1.0.0"}, "x", "2.0.0", None),
        (set(), "x", "1.0.0", None),
    ],
)
def test_pick_tag(tags, name, version, picked):
    assert up.pick_tag(tags, name, version) == picked


@pytest.mark.parametrize(
    ("expression", "ids"),
    [
        ("MIT", ["MIT"]),
        ("MIT OR Apache-2.0", ["MIT", "Apache-2.0"]),
        ("(MIT OR Apache-2.0) AND BSD-3-Clause", ["MIT", "Apache-2.0", "BSD-3-Clause"]),
        ("Apache-2.0 WITH LLVM-exception", ["Apache-2.0"]),
        ("GPL-2.0+", ["GPL-2.0"]),
        ("", []),
        (None, []),
    ],
)
def test_spdx_ids(expression, ids):
    assert up.spdx_ids(expression) == ids


def test_standard_text_says_where_it_came_from():
    text = up.standard_text("MIT OR Apache-2.0")
    assert text is not None
    assert text.startswith(f"SPDX License List {up.SPDX_LIST_VERSION} standard text")
    assert "Permission is hereby granted" in text
    assert "Apache License" in text


@pytest.mark.parametrize("expression", ["LicenseRef-Foo", "GPL-3.0-only", "", None])
def test_standard_text_is_none_without_a_bundled_text(expression):
    assert up.standard_text(expression) is None


def test_bundled_spdx_texts_are_the_published_ones():
    # sha256 of spdx/license-list-data text/<id>.txt at v3.29.0.
    expected = {
        "0BSD": "e3f18c71e10d673590eb9856c1d79dd3b4b0d65404efb5e8584dbede7edd608b",
        "Apache-2.0": "074e6e32c86a4c0ef8b3ed25b721ca23aca83df277cd88106ef7177c354615ff",
        "BSD-2-Clause": "f32fb3b417a194167cfad068223fc975ba96c5960513a10f66a3c28720aec1df",
        "BSD-3-Clause": "5a93d5831e1297ab10fe643e1a631e83be392896da14ee2951285a79012df69d",
        "BSL-1.0": "84c6ef3ea9e3254a54d0acf5d3e0c61ae011b8fef7dd6940591cf060e6380a8f",
        "CC0-1.0": "a2010f343487d3f7618affe54f789f5487602331c0a8d03f49e9a7c547cf0499",
        "CDLA-Permissive-2.0": (
            "4531a67d443284d93ffed0803df5b10634aff21c3d77e381f2d48af01d875868"
        ),
        "CNRI-Python": "69b5d7d41a6ece3b20c6a77774f567948766d8643f7646e3f65474692cce5c15",
        "EPL-2.0": "61616a4066701bc7888ad910de1633dbc55eb4d4d30d930feef55afb9492e33c",
        "HPND": "4d9ae4a36816338dfa153d1d6f92645c45f25eaa46908d3096922e2431fb571e",
        "ISC": "521c6f0ed8e64736684f89843d16a4df6b4b7449acbcfc6ca6630a3037ca8c53",
        "MIT-0": "59746d6285ffa44bfc7ecada352aa5d6a20dc8eab418a60ce091cc739012c135",
        "MIT": "b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5",
        "MPL-2.0": "66a3107d5ad6a058aab753eaac2047ccb2ed0e39465dd0fe5844da3e300d5172",
        "PSF-2.0": "ab745c5061d1dea43a3885e5b4b6befc7e983954954775c5736debeefcdfd89b",
        "Python-2.0": "893c2bafbb8133f7aa97e1f79a3ee3241ebca7025f56278e9e1f72bb98592f9d",
        "Unicode-3.0": "f7db81051789b729fea528a63ec4c938fdcb93d9d61d97dc8cc2e9df6d47f2a1",
        "Unicode-DFS-2016": "1a33fc12a3abb0b3eb14db70e36ef1d0af51c93b1cc1f80f022c3bd406e9b874",
        "Unlicense": "0bdebfeda07d45dada625ae1317c6f833186e798b171d0db640bcf32e92a8240",
        "X11": "e1963c9958b93bc2578b34f8ae22cfe205e8c0e7d5be7235cfdbdefbf92a5423",
        "Zlib": "bfb1112d49db5b1daecdfef24bd7e2f3ea0bafb33aa67aa0ab51e2bf8407c03d",
        "curl": "272d1239bdcef73bb98bac206168ca1040bd4a31296755fadfb7515bcea18df3",
    }
    actual = {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in up.SPDX_TEXTS.glob("*.txt")
    }
    assert actual == expected


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def upstream_repo(tmp_path):
    """A repository with a licence at the root and a crate subdirectory."""
    repo = tmp_path / "upstream"
    (repo / "crate-a").mkdir(parents=True)
    (repo / "LICENSE-MIT").write_text("MIT from the repo root\n", encoding="utf-8")
    (repo / "LICENSE-APACHE").write_text(
        "Apache from the repo root\n", encoding="utf-8"
    )
    (repo / "NOTICE").write_text("Root NOTICE\n", encoding="utf-8")
    (repo / "crate-a" / "src.rs").write_text("", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "crate-a-v1.2.0")
    return f"file://{repo}"


def test_fetch_from_reads_every_licence_file_at_the_version_tag(upstream_repo):
    got = up.fetch_from(upstream_repo, "crate-a", "crate-a", "1.2.0")
    assert got.reachable
    assert got.licence is not None
    first, _, body = got.licence.partition("\n\n")
    assert first == (
        f"Licence text from {upstream_repo} at tag crate-a-v1.2.0: "
        "LICENSE-APACHE, LICENSE-MIT. The published package ships none."
    )
    assert "LICENSE-MIT:\n\nMIT from the repo root" in body
    assert "LICENSE-APACHE:\n\nApache from the repo root" in body
    assert got.notice == "Root NOTICE"


def test_fetch_from_with_no_matching_tag_is_empty_and_reachable(upstream_repo):
    assert up.fetch_from(upstream_repo, "", "crate-a", "9.9.9") == up.Upstream()


def test_fetch_from_a_missing_repository_is_empty(tmp_path):
    got = up.fetch_from(f"file://{tmp_path / 'nothing'}", "", "x", "1.0.0")
    assert got.licence is None


def _entry(
    spdx, licence=None, repository="https://github.com/o/r", verdict=Verdict.OBLIGATIONS
):
    finding = Finding(
        "crates",
        "dep",
        "1.0.0",
        spdx,
        "metadata",
        "license",
        Mode.STATIC,
        verdict,
        (),
        "permissive",
    )
    return NoticeEntry(finding, licence, None, repository)


@pytest.mark.parametrize(
    ("upstream", "spdx", "starts"),
    [
        (
            up.Upstream("Licence text from repo\n\nMIT body"),
            "MIT",
            "Licence text from repo",
        ),
        (up.Upstream(), "MIT", "SPDX License List"),
        (up.Upstream(), "LicenseRef-Foo", None),
        (up.Upstream(reachable=False), "MIT", None),
    ],
)
def test_missing_text_comes_from_the_repo_then_spdx_never_after_a_network_failure(
    upstream, spdx, starts
):
    (filled,) = _with_upstream_texts([_entry(spdx)], lambda *_: upstream)
    text = filled.licence_text
    assert (text is None) if starts is None else (text or "").startswith(starts)


def test_a_package_that_ships_its_text_is_never_looked_up():
    def refuse(*_):
        raise AssertionError("looked up")

    (kept,) = _with_upstream_texts([_entry("MIT", licence="own text")], refuse)
    assert kept.licence_text == "own text"
