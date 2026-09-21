# Project:   HyperI CI
# File:      tests/unit/test_staleness.py
# Purpose:   A stale CLI must say so, once a day, without failing anything
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""issue #163: a build that is not the latest release has to report itself.

The payload in ``data/pypi-hyperi-ci-releases.json`` is captured from PyPI and
holds the floor move that caused the bug: 2.9.28 publishes ``>=3.12``, 2.10.0
publishes ``>=3.14``. A 3.12 project back-solves to 2.9.28 and is told nothing.
"""

import json
import sys
import time
from pathlib import Path

import pytest

from hyperi_ci import staleness

DATA = Path(__file__).parent / "data" / "pypi-hyperi-ci-releases.json"
CI_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE")
HERE = f"{sys.version_info.major}.{sys.version_info.minor}"
NEXT = f"{sys.version_info.major}.{sys.version_info.minor + 1}"


@pytest.fixture
def releases() -> dict[str, list]:
    """The captured PyPI releases mapping."""
    return json.loads(DATA.read_text(encoding="utf-8"))["releases"]


class TestTheCapturedPayload:
    """The fixture has to keep carrying the shape the code reads."""

    def test_the_floor_move_is_in_the_data(self, releases: dict[str, list]) -> None:
        assert staleness._latest_requires_python(releases, "2.9.28") == ">=3.12"
        assert staleness._latest_requires_python(releases, "2.10.0") == ">=3.14"

    def test_an_absent_release_names_no_floor(self, releases: dict[str, list]) -> None:
        assert staleness._latest_requires_python(releases, "9.9.9") is None

    def test_a_release_with_no_specifier_names_no_floor(self) -> None:
        assert staleness._latest_requires_python({"1.0": [{}]}, "1.0") is None

    def test_a_non_mapping_file_entry_is_skipped(self) -> None:
        payload: dict[str, list] = {"1.0": ["junk", {"requires_python": ">=3.14"}]}
        assert staleness._latest_requires_python(payload, "1.0") == ">=3.14"


class TestInterpreterBelow:
    """The floor comparison is what decides which warning gets written."""

    def test_the_running_interpreter_satisfies_its_own_version(self) -> None:
        assert staleness._interpreter_below(HERE) is False

    def test_a_higher_floor_is_out_of_reach(self) -> None:
        assert staleness._interpreter_below(NEXT) is True

    def test_a_patch_segment_is_ignored(self) -> None:
        assert staleness._interpreter_below(f"{HERE}.0") is False

    def test_an_unreadable_floor_is_not_treated_as_out_of_reach(self) -> None:
        # A floor that cannot be read must not manufacture the harsher
        # warning, which tells the reader their interpreter is the problem.
        assert staleness._interpreter_below("three.fourteen") is False


class TestStalenessLines:
    """What the operator actually reads."""

    def test_a_current_build_says_nothing(self) -> None:
        assert staleness.staleness_lines("2.10.3", "2.10.3", "3.14") == []

    def test_a_build_ahead_of_pypi_says_nothing(self) -> None:
        assert staleness.staleness_lines("2.11.0", "2.10.3", "3.14") == []

    def test_an_unparseable_running_version_says_nothing(self) -> None:
        assert staleness.staleness_lines("not-a-version", "2.10.3", "3.14") == []

    def test_it_names_both_versions_and_the_command(self) -> None:
        lines = staleness.staleness_lines("2.9.28", "2.10.3", HERE)
        expected = (
            f"Run the current one from any project: "
            f"uvx --python {HERE} hyperi-ci <command>"
        )
        assert "2.9.28" in lines[0]
        assert "2.10.3" in lines[0]
        assert lines[1] == expected
        assert lines[2] == "Or move the installed tool: hyperi-ci update"

    def test_an_out_of_reach_floor_is_named_as_the_reason(self) -> None:
        lines = staleness.staleness_lines("2.9.28", "2.10.3", NEXT)
        assert f"Python {HERE} cannot resolve it" in lines[0]
        assert f"needs Python >= {NEXT}" in lines[0]
        assert f"uvx --python {NEXT} hyperi-ci" in lines[1]

    def test_a_release_with_no_floor_drops_the_interpreter_pin(self) -> None:
        lines = staleness.staleness_lines("2.9.28", "2.10.3", None)
        expected = "Run the current one from any project: uvx hyperi-ci <command>"
        assert lines[1] == expected

    def test_every_line_is_ascii(self) -> None:
        for line in staleness.staleness_lines("2.9.28", "2.10.3", NEXT):
            assert line.isascii()


class TestLinesForReleases:
    """The decision, run against the captured payload and no network."""

    def test_the_bug_report_scenario(self, releases: dict[str, list]) -> None:
        lines = staleness.lines_for_releases(releases, "2.9.28")
        assert "2.9.28 is behind the latest release 2.10.3" in lines[0]

    def test_the_latest_release_reports_nothing(
        self, releases: dict[str, list]
    ) -> None:
        assert staleness.lines_for_releases(releases, "2.10.3") == []

    def test_the_pin_comes_from_the_latest_release_not_the_running_one(
        self, releases: dict[str, list]
    ) -> None:
        # The running build's own floor is >=3.12 and would resolve straight
        # back to itself, so the advice has to carry 2.10.3's floor.
        lines = staleness.lines_for_releases(releases, "2.9.28")
        assert "uvx --python 3.14 hyperi-ci" in lines[1]

    def test_an_empty_payload_reports_nothing(self) -> None:
        assert staleness.lines_for_releases({}, "2.9.28") == []


class TestTheDailyInterval:
    """Once a day, and never a timeout on every command when PyPI is gone."""

    @pytest.fixture(autouse=True)
    def _cache_in_tmp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            staleness, "TIMESTAMP_FILE", tmp_path / "cache" / "last-stale-check"
        )

    def test_a_first_run_is_due(self) -> None:
        assert staleness._check_is_due(time.time()) is True

    def test_a_check_just_recorded_is_not_due(self) -> None:
        now = time.time()
        staleness._record_check(now)
        assert staleness._check_is_due(now) is False

    def test_a_check_is_due_again_after_the_interval(self) -> None:
        now = time.time()
        staleness._record_check(now - staleness.CHECK_INTERVAL)
        assert staleness._check_is_due(now) is True

    def test_an_unreadable_record_is_due(self) -> None:
        staleness._record_check(time.time())
        staleness.TIMESTAMP_FILE.write_text("whenever", encoding="utf-8")
        assert staleness._check_is_due(time.time()) is True

    def test_the_record_is_written_where_it_survives_a_reboot(self) -> None:
        staleness._record_check(1234.5)
        assert staleness.TIMESTAMP_FILE.read_text(encoding="utf-8") == "1234.5"


class TestSuppression:
    """A warning nobody asked for in a place it cannot help is noise."""

    @pytest.fixture(autouse=True)
    def _cache_in_tmp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            staleness, "TIMESTAMP_FILE", tmp_path / "cache" / "last-stale-check"
        )

    def test_ci_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "true")
        assert staleness.warn_if_stale() == []

    def test_ci_does_not_even_record_a_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gate runs before the cache write, so a pinned CI install never
        # reaches PyPI and never pays the request.
        monkeypatch.setenv("CI", "true")
        staleness.warn_if_stale()
        assert not staleness.TIMESTAMP_FILE.exists()

    def test_the_kill_switch_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in CI_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv(staleness.SILENCE_ENV, "1")
        assert staleness.warn_if_stale() == []
        assert not staleness.TIMESTAMP_FILE.exists()

    def test_a_failure_anywhere_inside_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unwritable cache, a mangled payload, a DNS error: whatever breaks,
        # CLI entry gets a clean return rather than a traceback.
        def _explode(_now: float) -> list[str]:
            raise OSError("cache is unwritable")

        monkeypatch.setattr(staleness, "_check", _explode)
        assert staleness.warn_if_stale() == []
