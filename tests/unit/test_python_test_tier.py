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
from hyperi_ci.languages.python.test import (
    Skip,
    parse_skips,
    summary_counts,
    tier_detail,
)
from hyperi_ci.languages.tiering import SuiteTier

MODULE = "hyperi_ci.languages.python.test"

_PLAIN = """\
============================= test session starts ==============================
collected 4 items / 2 deselected / 2 selected

tests/test_probe.py::test_unit PASSED                                    [ 50%]
tests/test_probe.py::test_skipped SKIPPED (probe)                        [100%]

=========================== short test summary info ============================
SKIPPED [1] tests/test_probe.py:7: probe
================== 1 passed, 1 skipped, 2 deselected in 0.02s ==================
"""

# Real pytest 9.1.1 + pytest-xdist 3.8.0 output under -v -n 2 -rs, with the
# progress lines trimmed and the paths shortened.
_XDIST_SKIPS = """\
created: 2/2 workers
2 workers [8 items]

scheduling tests via LoadScheduling

[gw1] [ 12%] SKIPPED tests/test_a.py::test_kafka2
[gw0] [ 25%] PASSED tests/test_a.py::test_ok
[gw0] [100%] SKIPPED tests/test_a.py::test_noreason

=========================== short test summary info ============================
SKIPPED [1] tests/test_mod.py:3: whole module: no docker
SKIPPED [1] tests/test_a.py:13: needs kafka
SKIPPED [1] tests/test_a.py:18: linux only: x
SKIPPED [1] tests/test_a.py:8: needs kafka
SKIPPED [1] tests/test_a.py:28: ClickHouse not available
SKIPPED [1] tests/test_a.py:34: param skip 1
SKIPPED [1] tests/test_a.py:38: Skipped
SKIPPED [1] tests/test_a.py:34: param skip 2
========================= 1 passed, 8 skipped in 0.53s =========================
"""

# Real pytest 9.1.1 output under -qq -rs --no-fold-skipped.
_UNFOLDED_SKIPS = """\
.sss                                                                     [100%]
=========================== short test summary info ============================
SKIPPED tests/test_mod.py - Skipped: whole module: no docker
SKIPPED tests/test_a.py::test_kafka - Skipped: needs kafka
SKIPPED tests/test_a.py::test_noreason - Skipped
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


class TestParseSkips:
    def test_folded_lines(self) -> None:
        assert parse_skips(_PLAIN) == [Skip(1, "tests/test_probe.py:7", "probe")]

    def test_xdist_lists_every_skip(self) -> None:
        skips = parse_skips(_XDIST_SKIPS)
        assert sum(skip.count for skip in skips) == 8
        assert skips[0] == Skip(1, "tests/test_mod.py:3", "whole module: no docker")
        assert Skip(1, "tests/test_a.py:38", "Skipped") in skips

    def test_a_reason_may_carry_colons(self) -> None:
        assert Skip(1, "tests/test_a.py:18", "linux only: x") in parse_skips(
            _XDIST_SKIPS
        )

    def test_folded_count_above_one(self) -> None:
        text = "=== short test summary info ===\nSKIPPED [12] tests/t.py:4: no db\n"
        assert parse_skips(text) == [Skip(12, "tests/t.py:4", "no db")]

    def test_folded_without_a_line_number(self) -> None:
        text = "=== short test summary info ===\nSKIPPED [2] tests/t.py: no db\n"
        assert parse_skips(text) == [Skip(2, "tests/t.py", "no db")]

    def test_unfolded_lines_drop_the_skipped_prefix(self) -> None:
        assert parse_skips(_UNFOLDED_SKIPS) == [
            Skip(1, "tests/test_mod.py", "whole module: no docker"),
            Skip(1, "tests/test_a.py::test_kafka", "needs kafka"),
            Skip(1, "tests/test_a.py::test_noreason", "Skipped"),
        ]

    def test_lines_before_the_summary_are_not_skips(self) -> None:
        """A test's own output can print anything; only the summary counts."""
        text = "SKIPPED [1] fake.py:1: printed by a test\n" + _PLAIN
        assert parse_skips(text) == [Skip(1, "tests/test_probe.py:7", "probe")]

    def test_no_summary_section_means_no_skips_listed(self) -> None:
        assert parse_skips("=== 3 passed in 0.01s ===") == []

    def test_colour_codes_are_ignored(self) -> None:
        coloured = (
            "\x1b[36m\x1b[1m=== short test summary info ===\x1b[0m\n"
            "\x1b[33mSKIPPED\x1b[0m [1] tests/t.py:4: no db\n"
        )
        assert parse_skips(coloured) == [Skip(1, "tests/t.py:4", "no db")]


class _Recorder:
    def __init__(self, output: str = _PLAIN, rc: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.notices: list[tuple[SuiteTier, str]] = []
        self.errors: list[str] = []
        self.infos: list[str] = []
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
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    rec = _Recorder()
    monkeypatch.setattr(f"{MODULE}.shutil.which", lambda _tool: "/usr/bin/x")
    monkeypatch.setattr(f"{MODULE}._resolve_cmd", lambda cmd: cmd)
    monkeypatch.setattr(f"{MODULE}.stream_cmd", rec.stream)
    monkeypatch.setattr(f"{MODULE}.announce_tier", rec.announce)
    monkeypatch.setattr(f"{MODULE}.error", rec.errors.append)
    monkeypatch.setattr(f"{MODULE}.info", rec.infos.append)
    monkeypatch.setattr(f"{MODULE}.is_ci", lambda: False)
    return rec


def _config(python: dict[str, Any] | None = None, **test: Any) -> CIConfig:
    return CIConfig(
        _raw={
            "test": {
                "coverage": False,
                "python": {"parallel": False, **(python or {})},
                **test,
            }
        }
    )


def _allow(*patterns: str, **test: Any) -> CIConfig:
    return _config(full={"python": {"allow_skip": list(patterns)}}, **test)


class TestCommandPerTier:
    def test_core_command_is_unchanged(self, recorder: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "core"}) == 0
        assert recorder.commands == [["pytest", "-v", "--tb=short"]]

    def test_no_tier_given_is_core(self, recorder: _Recorder) -> None:
        assert py_test.run(_config()) == 0
        assert "-m" not in recorder.commands[0]

    def test_full_selects_everything_by_default(self, recorder: _Recorder) -> None:
        assert py_test.run(_allow("^probe$"), extra_env={"TEST_TIER": "full"}) == 0
        assert recorder.commands[0][-2:] == ["-m", ""]

    def test_full_uses_the_configured_expression(self, recorder: _Recorder) -> None:
        config = _config(
            full={"python": {"markers": "not live", "allow_skip": ["^probe$"]}}
        )
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
        config = _allow("^probe$", use_tiers=True)
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


class TestFullTierSkips:
    def test_a_skip_nobody_allowed_fails_full(self, recorder: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "full"}) == 1
        named = [m for m in recorder.errors if "probe" in m]
        assert len(named) == 1
        assert "1 skipped" in named[0]
        assert "tests/test_probe.py:7" in named[0]
        assert any("test.full.python.allow_skip" in m for m in recorder.errors)

    def test_an_allowed_skip_passes_and_is_named(self, recorder: _Recorder) -> None:
        assert py_test.run(_allow("prob"), extra_env={"TEST_TIER": "full"}) == 0
        assert recorder.errors == []
        assert any("probe" in m and "allow_skip" in m for m in recorder.infos)

    def test_the_pattern_is_searched_not_anchored(self, recorder: _Recorder) -> None:
        assert py_test.run(_allow("rob"), extra_env={"TEST_TIER": "full"}) == 0

    def test_a_pattern_that_misses_still_fails(self, recorder: _Recorder) -> None:
        assert py_test.run(_allow("^kafka"), extra_env={"TEST_TIER": "full"}) == 1

    def test_core_keeps_skips_as_skips(self, recorder: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "core"}) == 0
        assert recorder.errors == []

    def test_one_error_line_per_reason_with_its_total(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        monkeypatch.setattr(recorder, "_output", _XDIST_SKIPS)
        assert py_test.run(_allow("^param skip"), extra_env={"TEST_TIER": "full"}) == 1
        kafka = [m for m in recorder.errors if "needs kafka" in m]
        assert len(kafka) == 1
        assert "2 skipped" in kafka[0]
        assert "tests/test_a.py:13" in kafka[0]
        assert "tests/test_a.py:8" in kafka[0]
        assert not any("param skip" in m for m in recorder.errors)

    def test_many_locations_are_counted_past_the_first_few(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        lines = "".join(f"SKIPPED [1] tests/t.py:{n}: no db\n" for n in range(5))
        output = f"=== short test summary info ===\n{lines}5 skipped in 0.1s\n"
        monkeypatch.setattr(recorder, "_output", output)
        assert py_test.run(_config(), extra_env={"TEST_TIER": "full"}) == 1
        [line] = [m for m in recorder.errors if "no db" in m]
        assert "5 skipped" in line
        assert "tests/t.py:2 and 2 more" in line
        assert "tests/t.py:3" not in line

    def test_skips_the_summary_does_not_list_cannot_pass(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        """Counted but not listed means unchecked, which is not a pass."""
        unlisted = "=== 3 passed, 2 skipped in 0.1s ===\n"
        monkeypatch.setattr(recorder, "_output", unlisted)
        assert py_test.run(_allow(".*"), extra_env={"TEST_TIER": "full"}) == 1
        assert any("could not be checked" in m for m in recorder.errors)

    def test_no_summary_line_cannot_pass(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        monkeypatch.setattr(recorder, "_output", ".s.\n")
        assert py_test.run(_config(), extra_env={"TEST_TIER": "full"}) == 1
        assert any("could not be checked" in m for m in recorder.errors)

    def test_an_empty_run_with_a_module_skip_still_fails(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        """pytest exits 5 when a module-level skip leaves nothing collected."""
        output = (
            "=== short test summary info ===\n"
            "SKIPPED [1] tests/test_mod.py:3: no docker\n"
            "1 skipped in 0.00s\n"
        )
        monkeypatch.setattr(recorder, "_output", output)
        monkeypatch.setattr(recorder, "_rc", 5)
        assert py_test.run(_config(), extra_env={"TEST_TIER": "full"}) == 1

    def test_a_failing_run_keeps_its_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        monkeypatch.setattr(recorder, "_rc", 2)
        assert py_test.run(_config(), extra_env={"TEST_TIER": "full"}) == 2

    def test_a_non_list_allow_skip_fails_before_running(
        self, recorder: _Recorder
    ) -> None:
        config = _config(full={"python": {"allow_skip": "kafka"}})
        assert py_test.run(config, extra_env={"TEST_TIER": "full"}) == 1
        assert recorder.commands == []
        assert any("test.full.python.allow_skip" in m for m in recorder.errors)

    def test_a_bad_regex_fails_before_running(self, recorder: _Recorder) -> None:
        assert py_test.run(_allow("needs (kafka"), extra_env={"TEST_TIER": "full"}) == 1
        assert recorder.commands == []
        assert any("needs (kafka" in m for m in recorder.errors)

    def test_allow_skip_is_not_read_under_core(self, recorder: _Recorder) -> None:
        config = _config(full={"python": {"allow_skip": "kafka"}})
        assert py_test.run(config, extra_env={"TEST_TIER": "core"}) == 0


class TestReportChars:
    """``-r`` is last-wins, so full extends the project's chars with ``s``."""

    def test_core_passes_no_report_chars(self, recorder: _Recorder) -> None:
        py_test.run(_config(), extra_env={"TEST_TIER": "core"})
        assert not any(arg.startswith("-r") for arg in recorder.commands[0])

    def test_full_keeps_pytest_default_chars(self, recorder: _Recorder) -> None:
        py_test.run(_allow("probe"), extra_env={"TEST_TIER": "full"})
        assert "-rfEs" in recorder.commands[0]

    def test_full_extends_the_project_args(self, recorder: _Recorder) -> None:
        config = _config(
            python={"args": ["-v", "-ra"]},
            full={"python": {"allow_skip": ["probe"]}},
        )
        py_test.run(config, extra_env={"TEST_TIER": "full"})
        assert "-ras" in recorder.commands[0]

    def test_full_extends_the_project_config_file(
        self, recorder: _Recorder, tmp_path: Path
    ) -> None:
        (tmp_path / "pytest.ini").write_text(
            "[pytest]\naddopts = -rA\n", encoding="utf-8", newline="\n"
        )
        py_test.run(_allow("probe"), extra_env={"TEST_TIER": "full"})
        assert "-rAs" in recorder.commands[0]

    def test_full_extends_pytest_addopts(
        self, recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "-rx")
        py_test.run(_allow("probe"), extra_env={"TEST_TIER": "full"})
        assert "-rxs" in recorder.commands[0]


class TestSlowestTestsInCI:
    @pytest.fixture
    def in_ci(self, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> _Recorder:
        monkeypatch.setattr(f"{MODULE}.is_ci", lambda: True)
        return recorder

    def test_core_in_ci_reports_the_25_slowest(self, in_ci: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "core"}) == 0
        assert in_ci.commands == [["pytest", "-v", "--tb=short", "--durations=25"]]

    def test_full_in_ci_reports_them_too(self, in_ci: _Recorder) -> None:
        assert py_test.run(_allow("probe"), extra_env={"TEST_TIER": "full"}) == 0
        assert "--durations=25" in in_ci.commands[0]

    def test_a_local_run_is_left_alone(self, recorder: _Recorder) -> None:
        py_test.run(_config(), extra_env={"TEST_TIER": "core"})
        assert not any(a.startswith("--durations") for a in recorder.commands[0])

    def test_the_project_args_setting_it_win(self, in_ci: _Recorder) -> None:
        config = _config(python={"args": ["-v", "--durations", "5"]})
        py_test.run(config, extra_env={"TEST_TIER": "core"})
        assert in_ci.commands == [["pytest", "-v", "--durations", "5"]]

    def test_pytest_addopts_setting_it_wins(
        self, in_ci: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "--durations=0")
        py_test.run(_config(), extra_env={"TEST_TIER": "core"})
        assert "--durations=25" not in in_ci.commands[0]

    @pytest.mark.parametrize(
        ("filename", "text"),
        [
            (
                "pyproject.toml",
                '[tool.pytest.ini_options]\naddopts = "--durations=5"\n',
            ),
            ("pyproject.toml", '[tool.pytest]\naddopts = ["--durations=5"]\n'),
            ("pytest.toml", '[pytest]\naddopts = ["--durations=5"]\n'),
            (".pytest.toml", '[pytest]\naddopts = ["--durations=5"]\n'),
            ("pytest.ini", "[pytest]\naddopts = --durations 5\n"),
            (".pytest.ini", "[pytest]\naddopts = --durations 5\n"),
            ("tox.ini", "[pytest]\naddopts = --durations=5\n"),
            ("setup.cfg", "[tool:pytest]\naddopts = --durations=5\n"),
        ],
    )
    def test_a_config_file_setting_it_wins(
        self, in_ci: _Recorder, tmp_path: Path, filename: str, text: str
    ) -> None:
        (tmp_path / filename).write_text(text, encoding="utf-8", newline="\n")
        py_test.run(_config(), extra_env={"TEST_TIER": "core"})
        assert "--durations=25" not in in_ci.commands[0]

    def test_durations_min_alone_is_not_a_durations_setting(
        self, in_ci: _Recorder
    ) -> None:
        config = _config(python={"args": ["-v", "--durations-min=2"]})
        py_test.run(config, extra_env={"TEST_TIER": "core"})
        assert in_ci.commands[0][-1] == "--durations=25"


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
        monkeypatch.setattr(f"{MODULE}.echo_chunk", rec.infos.append)
        monkeypatch.setattr(f"{MODULE}.error", rec.errors.append)
        monkeypatch.setattr(f"{MODULE}.is_ci", lambda: False)
        return rec

    def test_ci_prints_the_slowest_and_the_counts_still_read(
        self, probe: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(f"{MODULE}.is_ci", lambda: True)
        assert py_test.run(_config(), extra_env={"TEST_TIER": "core"}) == 0
        assert "slowest 25 durations" in "".join(probe.infos)
        assert probe.notices == [(SuiteTier.CORE, "1 passed, 1 skipped, 2 deselected")]

    def test_core_keeps_the_project_selection(self, probe: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "core"}) == 0
        assert probe.notices == [(SuiteTier.CORE, "1 passed, 1 skipped, 2 deselected")]

    def test_full_overrides_addopts_and_deselects_nothing(
        self, probe: _Recorder
    ) -> None:
        assert py_test.run(_allow("^probe$"), extra_env={"TEST_TIER": "full"}) == 0
        assert probe.notices == [(SuiteTier.FULL, "3 passed, 1 skipped, 0 deselected")]

    def test_full_expression_can_keep_one_marker_out(self, probe: _Recorder) -> None:
        config = _config(
            full={"python": {"markers": "not live", "allow_skip": ["^probe$"]}}
        )
        assert py_test.run(config, extra_env={"TEST_TIER": "full"}) == 0
        assert probe.notices == [(SuiteTier.FULL, "2 passed, 1 skipped, 1 deselected")]

    def test_full_fails_on_a_skip_nobody_allowed(self, probe: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "full"}) == 1
        assert any("probe" in m and "tests/test_probe.py:" in m for m in probe.errors)

    def test_core_passes_the_same_skip(self, probe: _Recorder) -> None:
        assert py_test.run(_config(), extra_env={"TEST_TIER": "core"}) == 0
        assert probe.errors == []

    def test_xdist_workers_report_the_skip_reason(self, probe: _Recorder) -> None:
        config = _config(python={"args": ["-v", "-n", "2"]})
        assert py_test.run(config, extra_env={"TEST_TIER": "full"}) == 1
        assert any("probe" in m for m in probe.errors)
        assert "created: 2/2 workers" in "".join(probe.infos)

    def test_xdist_run_with_the_skip_allowed_passes(self, probe: _Recorder) -> None:
        config = _config(
            python={"args": ["-v", "-n", "2"]},
            full={"python": {"allow_skip": ["^probe$"]}},
        )
        assert py_test.run(config, extra_env={"TEST_TIER": "full"}) == 0

    def test_full_still_lists_failures_in_the_short_summary(
        self, probe: _Recorder, tmp_path: Path
    ) -> None:
        """A bare ``-rs`` would replace pytest's default ``fE`` and drop them."""
        (tmp_path / "tests" / "test_fails.py").write_text(
            "def test_broken():\n    assert False\n", encoding="utf-8"
        )
        assert py_test.run(_allow("^probe$"), extra_env={"TEST_TIER": "full"}) == 1
        assert "FAILED tests/test_fails.py::test_broken" in "".join(probe.infos)
