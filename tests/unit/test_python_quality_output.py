# Project:   HyperI CI
# File:      tests/unit/test_python_quality_output.py
# Purpose:   Tests for how a quality tool's output reaches the reader
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Quality-tool output tests.

A non-blocking tool emitted its findings with `print()` to stdout while every
surrounding message went through loguru to stderr. The two interleave, so
vulture's 111 advisory findings landed AFTER the verdict they preceded, and
the tail of a failed run was guaranteed to be unrelated to the cause (#212).
"""

import pytest

from hyperi_ci.languages.python import quality


class TestOutputGoesThroughOneStream:
    """Ordering is the defect. Two streams cannot be kept in order."""

    def test_nothing_is_emitted_for_empty_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(quality, "info", said.append)
        quality._emit_tool_output("vulture", "")
        quality._emit_tool_output("vulture", "   \n  ")
        quality._emit_tool_output("vulture", None)
        assert said == []

    def test_it_does_not_reach_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`print()` is what put these lines out of order in the first place."""
        monkeypatch.setattr(quality, "info", lambda _line: None)
        quality._emit_tool_output("vulture", "a finding")
        assert capsys.readouterr().out == ""


class TestAdvisoryNoiseIsCapped:
    """111 findings at 60 percent confidence buried a real failure."""

    def test_a_warn_tool_is_capped_and_says_how_many_it_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(quality, "info", said.append)
        findings = "\n".join(f"finding {i}" for i in range(111))
        quality._emit_tool_output("vulture", findings, cap=quality._WARN_OUTPUT_CAP)
        assert len(said) == quality._WARN_OUTPUT_CAP + 1
        assert "+86 more" in said[-1]
        assert "vulture" in said[-1]

    def test_output_under_the_cap_is_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(quality, "info", said.append)
        quality._emit_tool_output("ruff", "one\ntwo", cap=quality._WARN_OUTPUT_CAP)
        assert len(said) == 2
        assert not any("more from" in line for line in said)

    def test_a_blocking_failure_is_never_capped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the output someone has to act on to get the build back."""
        said: list[str] = []
        monkeypatch.setattr(quality, "info", said.append)
        quality._emit_tool_output("ruff", "\n".join(str(i) for i in range(200)))
        assert len(said) == 200
