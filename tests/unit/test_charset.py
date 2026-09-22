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

from hyperi_ci.quality.charset import BANNED, scan_text


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
