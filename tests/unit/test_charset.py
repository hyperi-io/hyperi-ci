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

from hyperi_ci.quality.charset import BANNED, INVISIBLE, scan_text


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
        from hyperi_ci.quality.charset import scan

        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "charset.py").write_text("# a — dash\n", encoding="utf-8")
        assert [f.rule for f in scan([tmp_path])] == ["charset/banned-typography"]
