# Project:   HyperI CI
# File:      tests/unit/test_mermaid_parse.py
# Purpose:   Cover mermaid block extraction, the structural screen and the gate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.quality.mermaid_parse`.

Extraction and the structural screen are pure, so they are tested directly on
real markdown. The grammar layer needs Node plus two npm packages; the tests
that need it skip when they are absent rather than asserting a false green, and
the DEGRADED path (they are absent) is tested unconditionally because that is
what most repos will actually hit.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import mermaid_parse

GOOD = "graph TD\n  A[Start] --> B[Done]"
BROKEN = "graph TD\n  A[Start --> B{{{"


def _config(**overrides: object) -> CIConfig:
    return CIConfig(_raw={"quality": {"mermaid_parse": "warn", **overrides}})


def _doc(tmp_path: Path, body: str, name: str = "doc.md") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _parser_available(tmp_path: Path) -> bool:
    """True when node resolves mermaid + linkedom for a repo at ``tmp_path``."""
    if shutil.which("node") is None:
        return False
    _, skipped = mermaid_parse._run_parser([], tmp_path)
    return skipped is None


class TestExtraction:
    """Fence handling, which is what decides whether a block is seen at all."""

    def test_a_fenced_block_is_found_with_its_opening_line(
        self, tmp_path: Path
    ) -> None:
        doc = _doc(tmp_path, f"# Title\n\nText.\n\n```mermaid\n{GOOD}\n```\n")
        blocks = mermaid_parse.extract_blocks(doc)
        assert len(blocks) == 1
        assert blocks[0].line == 5
        assert blocks[0].text == GOOD
        assert blocks[0].closed

    def test_a_non_mermaid_fence_is_ignored(self, tmp_path: Path) -> None:
        doc = _doc(tmp_path, "```python\nx = 1\n```\n")
        assert mermaid_parse.extract_blocks(doc) == []

    def test_several_blocks_in_one_file(self, tmp_path: Path) -> None:
        doc = _doc(tmp_path, f"```mermaid\n{GOOD}\n```\n\n```mermaid\n{GOOD}\n```\n")
        assert len(mermaid_parse.extract_blocks(doc)) == 2

    def test_an_unclosed_block_is_returned_as_unclosed(self, tmp_path: Path) -> None:
        doc = _doc(tmp_path, f"```mermaid\n{GOOD}\n")
        blocks = mermaid_parse.extract_blocks(doc)
        assert len(blocks) == 1
        assert not blocks[0].closed

    def test_a_tilde_fence_is_a_fence(self, tmp_path: Path) -> None:
        doc = _doc(tmp_path, f"~~~mermaid\n{GOOD}\n~~~\n")
        blocks = mermaid_parse.extract_blocks(doc)
        assert len(blocks) == 1
        assert blocks[0].closed

    def test_a_longer_outer_fence_is_not_closed_by_a_shorter_inner_one(
        self, tmp_path: Path
    ) -> None:
        doc = _doc(tmp_path, f"````mermaid\n```\n{GOOD}\n```\n````\n")
        blocks = mermaid_parse.extract_blocks(doc)
        assert len(blocks) == 1
        assert blocks[0].closed
        assert GOOD in blocks[0].text

    def test_an_info_string_with_attributes_still_matches(self, tmp_path: Path) -> None:
        doc = _doc(tmp_path, f"```mermaid title=x\n{GOOD}\n```\n")
        assert len(mermaid_parse.extract_blocks(doc)) == 1

    def test_an_unreadable_file_yields_nothing(self, tmp_path: Path) -> None:
        assert mermaid_parse.extract_blocks(tmp_path / "absent.md") == []


class TestScreen:
    """The structural faults, which the grammar layer cannot report."""

    def test_an_unclosed_block_is_an_error(self, tmp_path: Path) -> None:
        block = mermaid_parse.Block(tmp_path / "d.md", 1, GOOD, closed=False)
        finding = mermaid_parse.screen(block)
        assert finding is not None
        assert finding.rule == "mermaid/unclosed-fence"
        assert finding.level == "error"

    def test_an_empty_block_is_an_error(self, tmp_path: Path) -> None:
        block = mermaid_parse.Block(tmp_path / "d.md", 1, "   \n  ", closed=True)
        finding = mermaid_parse.screen(block)
        assert finding is not None
        assert finding.rule == "mermaid/empty-block"

    def test_a_well_formed_block_passes(self, tmp_path: Path) -> None:
        block = mermaid_parse.Block(tmp_path / "d.md", 1, GOOD, closed=True)
        assert mermaid_parse.screen(block) is None

    def test_no_diagram_keyword_list_is_consulted(self, tmp_path: Path) -> None:
        # Grammar belongs to the parser. A block naming a type this code has
        # never heard of must not be reported by the screen.
        block = mermaid_parse.Block(tmp_path / "d.md", 1, "brandNewType\n  a\n", True)
        assert mermaid_parse.screen(block) is None


class TestRun:
    """Gate semantics, including the degraded path where Node is absent."""

    def test_no_blocks_is_a_clean_skip(self, tmp_path: Path) -> None:
        doc = _doc(tmp_path, "# Just prose\n")
        assert mermaid_parse.run([doc], _config(), root=tmp_path) == 0

    def test_disabled_runs_nothing(self, tmp_path: Path) -> None:
        doc = _doc(tmp_path, f"```mermaid\n{BROKEN}\n")
        assert (
            mermaid_parse.run([doc], _config(mermaid_parse="disabled"), root=tmp_path)
            == 0
        )

    def test_structural_faults_gate_without_any_toolchain(self, tmp_path: Path) -> None:
        # An unclosed fence is caught with nothing installed, so this holds
        # whether or not the machine can run the parser.
        doc = _doc(tmp_path, f"```mermaid\n{GOOD}\n")
        rc = mermaid_parse.run([doc], _config(mermaid_parse="blocking"), root=tmp_path)
        assert rc == 1

    def test_structural_faults_only_warn_at_warn(self, tmp_path: Path) -> None:
        doc = _doc(tmp_path, f"```mermaid\n{GOOD}\n")
        assert mermaid_parse.run([doc], _config(), root=tmp_path) == 0

    def test_a_missing_parser_is_not_fatal_off_ci(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A dev box without Node must not fail a blocking check; CI is where a
        # check that could not run counts as unproven.
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        doc = _doc(tmp_path, f"```mermaid\n{GOOD}\n```\n")
        assert (
            mermaid_parse.run([doc], _config(mermaid_parse="blocking"), root=tmp_path)
            == 0
        )


class TestGrammarLayer:
    """The real mermaid parser, skipped where its packages are not installed."""

    @pytest.fixture(autouse=True)
    def _require_parser(self, tmp_path: Path) -> None:
        if not _parser_available(Path.cwd()):
            pytest.skip("node cannot resolve mermaid + linkedom")

    def test_a_valid_flowchart_passes(self, tmp_path: Path) -> None:
        # The case that fails without the DOM shim: DOMPurify.addHook throws on
        # a flowchart, so a green here proves the shim is doing its job.
        doc = _doc(tmp_path, f"```mermaid\n{GOOD}\n```\n")
        assert (
            mermaid_parse.run([doc], _config(mermaid_parse="blocking"), root=Path.cwd())
            == 0
        )

    def test_a_broken_flowchart_fails(self, tmp_path: Path) -> None:
        doc = _doc(tmp_path, f"```mermaid\n{BROKEN}\n```\n")
        assert (
            mermaid_parse.run([doc], _config(mermaid_parse="blocking"), root=Path.cwd())
            == 1
        )

    def test_a_sequence_diagram_passes(self, tmp_path: Path) -> None:
        body = "sequenceDiagram\n  Alice->>Bob: hi\n  Bob-->>Alice: yo"
        doc = _doc(tmp_path, f"```mermaid\n{body}\n```\n")
        assert mermaid_parse.run([doc], _config(), root=Path.cwd()) == 0


class TestRunnerIsShipped:
    """The .mjs is package data; a wheel without it silently loses the layer."""

    def test_the_runner_sits_beside_the_module(self) -> None:
        assert mermaid_parse.RUNNER.is_file()

    def test_the_runner_is_ascii(self) -> None:
        mermaid_parse.RUNNER.read_text(encoding="ascii")

    def test_the_wheel_carries_it(self) -> None:
        """`packages = ["src/hyperi_ci"]` carries the .mjs - assert, do not assume."""
        root = Path(__file__).resolve().parents[2]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        assert 'packages = ["src/hyperi_ci"]' in pyproject
        assert mermaid_parse.RUNNER.is_relative_to(root / "src" / "hyperi_ci")
