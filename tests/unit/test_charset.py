# Project:   HyperI CI
# File:      tests/unit/test_charset.py
# Purpose:   Tests for the banned-typography check
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Banned-typography tests.

The cases that decide whether this check survives are the ones it must NOT
fire on. A charset check that flags a name with a diacritic gets disabled
within a week, and then the rule it enforces decays again.
"""

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import charset
from hyperi_ci.quality.charset import BANNED, INVISIBLE, scan, scan_text


class TestItCatchesTheSubstitutions:
    def test_an_em_dash_is_reported(self) -> None:
        found = scan_text("f.py", "# License:   BUSL-1.1 — HYPERI")
        assert [f.rule for f in found] == ["charset/banned-typography"]
        assert "'--'" in found[0].message

    def test_the_line_number_is_the_one_to_fix(self) -> None:
        found = scan_text("f.py", "clean\nclean\nbad — here\n")
        assert found[0].line == 3

    def test_several_on_one_line_are_one_finding(self) -> None:
        """One finding per line, listing every swap -- not one per character."""
        found = scan_text("f.py", "‘a’ and “b”")
        assert len(found) == 1
        assert found[0].message.count("->") == 4

    def test_box_drawing_is_its_own_rule(self) -> None:
        found = scan_text("f.py", "#   a ──► b")
        rules = {f.rule for f in found}
        assert "charset/ascii-art" in rules
        assert any("mermaid" in f.message for f in found)

    def test_every_banned_character_is_caught(self) -> None:
        for char in BANNED:
            assert scan_text("f.py", f"x {char} y"), f"{char!r} not reported"

    def test_a_minus_sign_reads_as_a_hyphen_and_is_still_caught(self) -> None:
        """U+2212 renders as `-`, so only the check tells the two apart."""
        found = scan_text("f.py", "# range \N{MINUS SIGN}1 to 1")
        assert [f.rule for f in found] == ["charset/banned-typography"]


class TestTheSpacesThatAreNotSpaces:
    """These change what a parser does, which no other entry in the table has."""

    def test_every_invisible_space_is_caught(self) -> None:
        for char in INVISIBLE:
            assert scan_text("f.yaml", f"key:{char}value"), f"{char!r} not reported"

    def test_it_is_its_own_rule_and_names_the_character(self) -> None:
        found = scan_text("f.yaml", "retries:\N{NO-BREAK SPACE}3")
        assert [f.rule for f in found] == ["charset/invisible-space"]
        assert "NO-BREAK SPACE" in found[0].message

    def test_a_real_space_is_clean(self) -> None:
        assert scan_text("f.yaml", "retries: 3\n") == []


class TestWhatItMustLeaveAlone:
    def test_plain_ascii_is_clean(self) -> None:
        assert scan_text("f.py", "# License:   BUSL-1.1 - HYPERI\nx = 1\n") == []

    def test_a_diacritic_is_not_typography(self) -> None:
        """A name is not a substitution -- flagging it would get this disabled."""
        assert scan_text("f.py", '# author = "José Alvarez"') == []

    def test_the_replacement_character_is_left_alone(self) -> None:
        """`errors="replace"` output is deliberate, and documented as such."""
        assert scan_text("f.py", "# prints � instead of raising") == []

    def test_a_maths_glyph_is_left_alone(self) -> None:
        assert scan_text("f.py", "# where n ≥ 2 and x ≠ 0") == []


class TestTheSelfSkipIsExactlyThisFile:
    """Matching by NAME would exempt any consumer module called charset.py."""

    def test_another_charset_py_is_still_scanned(self, tmp_path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "charset.py").write_text("# a — dash\n", encoding="utf-8")
        assert [f.rule for f in scan([tmp_path])] == ["charset/banned-typography"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A repo with one real defect and one registry name that is correct DATA.

    Returns the lines charset logs; loguru bypasses capsys.
    """
    monkeypatch.chdir(tmp_path)
    for name in (
        "GITHUB_ACTIONS",
        "GITHUB_STEP_SUMMARY",
        "HYPERCI_QUALITY_STRICT",
        "HYPERCI_QUALITY_SKIP",
    ):
        monkeypatch.delenv(name, raising=False)
    _write(tmp_path / "src/pkg/code.py", "# a — dash\n")
    _write(
        tmp_path / "src/scalo/data/national_ids.toml",
        'name = "Identifiant Commun de l\N{RIGHT SINGLE QUOTATION MARK}Entreprise"\n',
    )
    lines: list[str] = []
    for level in ("info", "warn", "error", "success"):
        monkeypatch.setattr(charset, level, lines.append)
    return lines


def _config(**quality: object) -> CIConfig:
    return CIConfig(_raw={"quality": quality} if quality else {})


def _excluded_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if "excluded" in line]


class TestNoExclusionChangesNothing:
    def test_findings_match_a_plain_walk_of_the_tree(self, tmp_path: Path) -> None:
        """Every eligible file, in sorted order, and nothing else."""
        _write(tmp_path / "b/one.py", "x — y\n")
        _write(tmp_path / "a/.hidden/two.yaml", "k: ‘v’\n")
        _write(tmp_path / "a/three.md", "x — y\n")
        _write(tmp_path / "four.toml", "x = 1\n")
        expected = []
        for path in sorted(tmp_path.rglob("*")):
            if path.suffix in charset.SUFFIXES and path.is_file():
                expected.extend(scan_text(str(path), path.read_text(encoding="utf-8")))
        assert expected
        assert scan([tmp_path]) == expected

    def test_no_exclusion_prints_no_excluded_line(self, repo: list[str]) -> None:
        assert charset.run(_config(), roots=[Path("src")]) == 0
        assert _excluded_lines(repo) == []
        assert any("2 line(s)" in line for line in repo), repo


class TestExcludePaths:
    """`quality.exclude_paths` means what it means for every other tool."""

    def test_a_bare_name_excludes_that_directory_anywhere(
        self, repo: list[str], tmp_path: Path
    ) -> None:
        # get_exclude_dirs keeps an entry only when it is a directory at the
        # repo root, for every tool alike.
        (tmp_path / "data").mkdir()
        charset.run(_config(exclude_paths=["data"]), roots=[Path("src")])
        assert _excluded_lines(repo) == [
            "  charset: 1 file(s) excluded (quality.exclude_paths: 1)"
        ]
        assert any("1 line(s)" in line for line in repo), repo

    def test_a_relative_path_excludes_that_directory(self, repo: list[str]) -> None:
        charset.run(_config(exclude_paths=["src/scalo/data"]), roots=[Path("src")])
        assert _excluded_lines(repo) == [
            "  charset: 1 file(s) excluded (quality.exclude_paths: 1)"
        ]

    def test_a_partial_path_excludes_nothing(self, repo: list[str]) -> None:
        charset.run(_config(exclude_paths=["scalo/data"]), roots=[Path("src")])
        assert _excluded_lines(repo) == []
        assert any("2 line(s)" in line for line in repo), repo

    def test_a_scan_root_can_itself_be_excluded(self, repo: list[str]) -> None:
        assert charset.run(_config(exclude_paths=["src"]), roots=[Path("src")]) == 0
        assert _excluded_lines(repo) == [
            "  charset: 2 file(s) excluded (quality.exclude_paths: 2)"
        ]


class TestCharsetExclude:
    def test_double_star_spans_directories(
        self, repo: list[str], tmp_path: Path
    ) -> None:
        _write(tmp_path / "src/scalo/data/nested/more.toml", "x = “y”\n")
        rc = charset.run(
            _config(charset="blocking", charset_exclude=["src/*/data/**"]),
            roots=[Path("src")],
        )
        assert rc == 1, "the real defect in code.py still blocks"
        assert _excluded_lines(repo) == [
            "  charset: 2 file(s) excluded (quality.charset_exclude: 2)"
        ]

    def test_the_advisory_clears_once_the_data_is_excluded(
        self, repo: list[str], tmp_path: Path
    ) -> None:
        (tmp_path / "src/pkg/code.py").write_text("# a - dash\n", encoding="utf-8")
        charset.run(_config(charset_exclude=["src/*/data/**"]), roots=[Path("src")])
        assert "  charset: no banned typography" in repo

    def test_a_pattern_matches_the_whole_path(self, repo: list[str]) -> None:
        """`*.toml` names a top-level file; `**/*.toml` is the any-depth form."""
        charset.run(_config(charset_exclude=["*.toml"]), roots=[Path("src")])
        assert _excluded_lines(repo) == []

    def test_each_source_is_counted_once(self, repo: list[str], tmp_path: Path) -> None:
        _write(tmp_path / "src/pkg/vendored.yaml", "k: “v”\n")
        charset.run(
            _config(
                exclude_paths=["src/scalo/data"],
                charset_exclude=["src/pkg/*.yaml", "src/**/*.toml"],
            ),
            roots=[Path("src")],
        )
        assert _excluded_lines(repo) == [
            "  charset: 2 file(s) excluded "
            "(quality.charset_exclude: 1, quality.exclude_paths: 1)"
        ]

    @pytest.mark.parametrize(
        "value",
        [
            "src/*/data/**",
            {"paths": ["src/**"]},
            None,
            [3],
            [""],
            ["/src/**"],
        ],
    )
    def test_a_malformed_value_fails_the_stage(
        self, repo: list[str], value: object
    ) -> None:
        assert charset.run(_config(charset_exclude=value), roots=[Path("src")]) == 1
        assert any("quality.charset_exclude" in line for line in repo), repo


class TestDefaultPruning:
    def test_a_vendored_tree_is_not_scanned(
        self, repo: list[str], tmp_path: Path
    ) -> None:
        """Same always-pruned set as every other discovery in quality/targets.py."""
        _write(tmp_path / "src/pkg/node_modules/dep/index.mjs", "// a — b\n")
        charset.run(_config(), roots=[Path("src")])
        assert any("2 line(s)" in line for line in repo), repo
        assert _excluded_lines(repo) == []
