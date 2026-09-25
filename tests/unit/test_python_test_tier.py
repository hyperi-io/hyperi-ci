# Project:   HyperI CI
# File:      tests/unit/test_python_test_tier.py
# Purpose:   pytest command per test tier, and the tier notice counts
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import sys
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.python import test as py_test
from hyperi_ci.languages.python.test import summary_counts, tier_detail
from hyperi_ci.languages.tiering import SuiteTier

MODULE = "hyperi_ci.languages.python.test"

_PLAIN = """\
============================= test session starts ==============================
collected 4 items / 2 deselected / 2 selected

tests/test_probe.py::test_unit PASSED                                    [ 50%]
tests/test_probe.py::test_skipped SKIPPED (probe)                        [100%]

================== 1 passed, 1 skipped, 2 deselected in 0.02s ==================
"""

_QUIET = ".s                                                [100%]\n1 passed, 1 skipped, 2 deselected in 0.01s\n"

# Real pytest-xdist 3.8.0 output: the deselected count is absent.
_XDIST = """\
created: 2/2 workers
2 workers [2 items]

[gw0] [ 50%] PASSED tests/test_probe.py::test_unit
[gw1] [100%] SKIPPED tests/test_probe.py::test_skipped

========================= 1 passed, 1 skipped in 0.59s =========================
"""

_LONG_RUN = (
    "==== 2 failed, 7890 passed, 17 skipped, 281 deselected, 3 warnings, "
    "1 error in 865.30s (0:14:25) ===="
)


class TestSummaryCounts:
    def test_verbose_rule_line(self) -> None:
        assert summary_counts(_PLAIN) == {"passed": 1, "skipped": 1, "deselected": 2}

    def test_quiet_line(self) -> None:
        assert summary_counts(_QUIET) == {"passed": 1, "skipped": 1, "deselected": 2}

    def test_every_outcome_and_a_long_duration(self) -> None:
        assert summary_counts(_LONG_RUN) == {
            "failed": 2,
            "passed": 7890,
            "skipped": 17,
            "deselected": 281,
            "warnings": 3,
            "error": 1,
        }

    def test_all_deselected(self) -> None:
        assert summary_counts("=== 4 deselected in 0.02s ===") == {"deselected": 4}

    def test_no_tests_ran(self) -> None:
        assert summary_counts("=== no tests ran in 0.01s ===") == {}

    def test_no_summary(self) -> None:
        assert summary_counts("ImportError while loading conftest\n") is None

    def test_colour_codes_do_not_hide_the_counts(self) -> None:
        """PY_COLORS=1 colours the summary even into a pipe."""
        coloured = (
            "\x1b[32m=== \x1b[0m\x1b[32m\x1b[1m1 passed\x1b[0m, "
            "\x1b[33m1 skipped\x1b[0m, \x1b[33m2 deselected\x1b[0m"
            "\x1b[32m in 0.02s\x1b[0m\x1b[32m ===\x1b[0m"
        )
        assert summary_counts(coloured) == {
            "passed": 1,
            "skipped": 1,
            "deselected": 2,
        }

    def test_the_last_summary_wins_over_test_output(self) -> None:
        output = "a test printed: 9 passed in 1s\n" + _PLAIN
        assert summary_counts(output) == {
            "passed": 1,
            "skipped": 1,
            "deselected": 2,
        }


class TestTierDetail:
    def test_names_the_deselected_count(self) -> None:
        assert tier_detail(_PLAIN) == "1 passed, 1 skipped, 2 deselected"

    def test_zero_deselected_is_stated_not_omitted(self) -> None:
        detail = tier_detail("=== 3 passed, 1 skipped in 0.01s ===")
        assert detail == "3 passed, 1 skipped, 0 deselected"

    def test_failures_are_named(self) -> None:
        assert tier_detail(_LONG_RUN).startswith("7890 passed, 2 failed, 17 skipped")

    def test_xdist_says_it_cannot_count_deselected(self) -> None:
        """Reporting 0 under -n would claim nothing was left out."""
        assert tier_detail(_XDIST) == (
            "1 passed, 1 skipped, deselected count not reported under pytest-xdist"
        )

    def test_missing_summary_is_unknown_not_zero(self) -> None:
        assert "counts unknown" in tier_detail("Traceback ...\n")


class _Recorder:
    def __init__(self, output: str = _PLAIN, rc: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.notices: list[tuple[SuiteTier, str]] = []
        self._output = output
        self._rc = rc

    def stream(self, cmd: list[str], *, on_line: Any = None, **_kw: Any) -> Any:
        self.commands.append(cmd)
        for line in self._output.splitlines():
            on_line(line)
        return self._rc, self._output

    def announce(self, tier: SuiteTier, detail: str) -> None:
        self.notices.append((tier, detail))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Recorder:
    monkeypatch.chdir(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(f"{MODULE}.shutil.which", lambda _tool: "/usr/bin/x")
    monkeypatch.setattr(f"{MODULE}._resolve_cmd", lambda cmd: cmd)
    monkeypatch.setattr(f"{MODULE}.stream_cmd", rec.stream)
    monkeypatch.setattr(f"{MODULE}.announce_tier", rec.announce)
    return rec


def _config(**test: Any) -> CIConfig:
    return CIConfig(
        _raw={"test": {"coverage": False, "python": {"parallel": False}, **test}}
    )


class TestCommandPerTier:
    def test_core_command_is_unchanged(self, recorder: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "core"}) == 0
        assert recorder.commands == [["pytest", "-v", "--tb=short"]]

    def test_no_tier_given_is_core(self, recorder: _Recorder) -> None:
        assert py_test.run(_config()) == 0
        assert "-m" not in recorder.commands[0]

    def test_full_selects_everything_by_default(self, recorder: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "full"}) == 0
        assert recorder.commands[0][-2:] == ["-m", ""]

    def test_full_uses_the_configured_expression(self, recorder: _Recorder) -> None:
        config = _config(full={"python": {"markers": "not live"}})
        assert py_test.run(config, extra_env={"TEST_TIER": "full"}) == 0
        assert recorder.commands[0][-2:] == ["-m", "not live"]

    def test_markers_apply_only_to_full(self, recorder: _Recorder) -> None:
        config = _config(full={"python": {"markers": "not live"}})
        assert py_test.run(config, extra_env={"TEST_TIER": "core"}) == 0
        assert "-m" not in recorder.commands[0]

    def test_a_non_string_expression_fails_before_running(
        self, recorder: _Recorder
    ) -> None:
        config = _config(full={"python": {"markers": ["live"]}})
        assert py_test.run(config, extra_env={"TEST_TIER": "full"}) == 1
        assert recorder.commands == []

    def test_directory_split_carries_the_override_to_every_run(
        self, recorder: _Recorder
    ) -> None:
        config = _config(use_tiers=True)
        assert py_test.run(config, extra_env={"TEST_TIER": "full"}) == 0
        assert [cmd[-3:] for cmd in recorder.commands] == [
            ["-m", "", "tests/unit/"],
            ["-m", "", "tests/integration/"],
        ]


class TestEveryRunIsAnnounced:
    def test_core_notice_names_the_deselected_count(self, recorder: _Recorder) -> None:
        py_test.run(_config(), extra_env={"TEST_TIER": "core"})
        assert recorder.notices == [
            (SuiteTier.CORE, "1 passed, 1 skipped, 2 deselected")
        ]

    def test_a_failed_run_is_announced_too(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        failing = _Recorder(output="=== 1 failed, 1 passed in 0.1s ===", rc=1)
        monkeypatch.setattr(f"{MODULE}.stream_cmd", failing.stream)
        monkeypatch.setattr(f"{MODULE}.announce_tier", failing.announce)
        assert py_test.run(_config(), extra_env={"TEST_TIER": "full"}) == 1
        assert failing.notices == [
            (SuiteTier.FULL, "1 passed, 1 failed, 0 skipped, 0 deselected")
        ]

    def test_directory_runs_are_labelled(self, recorder: _Recorder) -> None:
        py_test.run(_config(use_tiers=True), extra_env={"TEST_TIER": "core"})
        assert [detail.split(":")[0] for _, detail in recorder.notices] == [
            "unit",
            "integration",
        ]


class TestAgainstRealPytest:
    """The -m override is pytest's behaviour, so check it against pytest."""

    @pytest.fixture
    def probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n"
            'testpaths = ["tests"]\n'
            "addopts = \"-m 'not integration and not live'\"\n"
            'markers = ["integration: x", "live: y"]\n',
            encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_probe.py").write_text(
            "import pytest\n\n"
            "def test_unit():\n    pass\n\n"
            "@pytest.mark.skip(reason='probe')\ndef test_skipped():\n    pass\n\n"
            "@pytest.mark.integration\ndef test_integration():\n    pass\n\n"
            "@pytest.mark.live\ndef test_live():\n    pass\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        monkeypatch.setattr(
            f"{MODULE}._resolve_cmd",
            lambda cmd: [sys.executable, "-m", *cmd, "-p", "no:cacheprovider"],
        )
        rec = _Recorder()
        monkeypatch.setattr(f"{MODULE}.announce_tier", rec.announce)
        monkeypatch.setattr(f"{MODULE}.echo_chunk", lambda _text: None)
        return rec

    def test_core_keeps_the_project_selection(self, probe: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "core"}) == 0
        assert probe.notices == [(SuiteTier.CORE, "1 passed, 1 skipped, 2 deselected")]

    def test_full_overrides_addopts_and_deselects_nothing(
        self, probe: _Recorder
    ) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "full"}) == 0
        assert probe.notices == [(SuiteTier.FULL, "3 passed, 1 skipped, 0 deselected")]

    def test_full_expression_can_keep_one_marker_out(self, probe: _Recorder) -> None:
        config = _config(full={"python": {"markers": "not live"}})
        assert py_test.run(config, extra_env={"TEST_TIER": "full"}) == 0
        assert probe.notices == [(SuiteTier.FULL, "2 passed, 1 skipped, 1 deselected")]
