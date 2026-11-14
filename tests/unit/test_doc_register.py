# Project:   HyperI CI
# File:      tests/unit/test_doc_register.py
# Purpose:   Cover the REGISTER-word docs lint
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.quality.doc_register`."""

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import doc_register


def _config(mode: str = "warn") -> CIConfig:
    return CIConfig(_raw={"quality": {"doc_register": mode}})


def _doc(tmp_path: Path, body: str, name: str = "doc.md") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("body", "lines"),
    [
        pytest.param("A robust parser.\n", [1], id="prose"),
        pytest.param("# A seam heading\n", [1], id="heading"),
        pytest.param("one\ntwo is robust\n", [2], id="softbreak-line"),
        pytest.param("| a | b |\n|---|---|\n| x | robust |\n", [3], id="table-cell"),
        pytest.param("- item is robust\n", [1], id="list-item"),
        pytest.param("A [robust link](https://x.example) here.\n", [1], id="link-text"),
        pytest.param("Plain prose.\n", [], id="clean"),
        pytest.param("Uses `robust` in code.\n", [], id="code-span"),
        pytest.param("```\nrobust\n```\n", [], id="fence"),
        pytest.param("Text.\n\n    robust indented\n", [], id="indented-code"),
        pytest.param(
            "A [link](https://robust.example/seam) here.\n", [], id="link-url"
        ),
        pytest.param("<!--\nrobust\n-->\n", [], id="html-comment"),
        pytest.param("---\ntitle: robust\n---\n\nText.\n", [], id="front-matter"),
        pytest.param("It skips vendor/ trees.\n", [], id="path-token"),
        pytest.param(
            "A robust line <!-- prose-disable-line register -->\n",
            [],
            id="disable-line",
        ),
        pytest.param(
            "<!-- prose-disable-line register -->\nA robust line\n",
            [2],
            id="disable-line-is-its-own-line",
        ),
        pytest.param(
            "<!-- prose-disable register -->\n\nA robust line.\n", [], id="disable-file"
        ),
        pytest.param(
            "<!-- prose-disable -->\n\nA robust line.\n", [], id="bare-disable"
        ),
        pytest.param(
            "<!-- prose-disable DOC-DIARY-3 -->\n\nA robust line.\n",
            [3],
            id="disable-other-rule",
        ),
        pytest.param(
            "```\n<!-- prose-disable -->\n```\n\nA robust line.\n",
            [5],
            id="disable-in-a-fence-is-quoted",
        ),
    ],
)
def test_scan(tmp_path: Path, body: str, lines: list[int]) -> None:
    found = doc_register.scan(_doc(tmp_path, body), tmp_path)
    assert [f.line for f in found] == lines
    assert all(f.rule == "register" and f.path == "doc.md" for f in found)


def test_an_unreadable_file_yields_nothing(tmp_path: Path) -> None:
    assert doc_register.scan(tmp_path / "gone.md", tmp_path) == []


@pytest.mark.parametrize(
    ("mode", "body", "rc"),
    [
        pytest.param("warn", "A robust parser.\n", 0, id="warn-never-fails"),
        pytest.param("blocking", "A robust parser.\n", 1, id="blocking-fails-on-a-hit"),
        pytest.param("blocking", "Plain prose.\n", 0, id="blocking-passes-clean"),
        pytest.param("disabled", "A robust parser.\n", 0, id="disabled-reads-nothing"),
    ],
)
def test_run_modes(tmp_path: Path, mode: str, body: str, rc: int) -> None:
    doc = _doc(tmp_path, body)
    assert doc_register.run([doc], _config(mode), root=tmp_path) == rc


def test_the_changelog_is_skipped(tmp_path: Path) -> None:
    doc = _doc(tmp_path, "- fix: a robust parser\n", name="CHANGELOG.md")
    assert doc_register.run([doc], _config("blocking"), root=tmp_path) == 0
