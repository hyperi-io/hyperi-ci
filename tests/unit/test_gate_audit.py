# Project:   HyperI CI
# File:      tests/unit/test_gate_audit.py
# Purpose:   A skipped gate must not read as a passing gate (issue #96)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Gate-audit tests.

The job shapes here are the real ones GitHub returns for a consumer calling a
reusable workflow -- `ci / Quality`, and a matrixed `ci / Test (runner)`. No
repo is contacted: `scan_runs` takes its jobs lookup as an argument precisely
so the walk is testable without a network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from hyperi_ci import gate_audit
from hyperi_ci.gate_audit import (
    GATE_JOBS,
    PRERELEASE_CHANNELS,
    Finding,
    RepoReport,
    audit_runs,
    gate_of,
    scan_runs,
)

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


def _ts(days_ago: float) -> str:
    """A GitHub timestamp that many days before NOW."""
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(run_id: int, days_ago: float) -> dict:
    return {
        "id": run_id,
        "updated_at": _ts(days_ago),
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


def _job(name: str, conclusion: str | None, days_ago: float) -> dict:
    return {"name": name, "conclusion": conclusion, "completed_at": _ts(days_ago)}


class TestGateOf:
    """Job-name matching, including the shapes that broke naive matching."""

    def test_plain_name(self) -> None:
        assert gate_of("Quality") == "Quality"

    def test_strips_the_calling_job_prefix(self) -> None:
        # A reusable workflow's jobs are reported as `<caller> / <job>`.
        assert gate_of("ci / Quality") == "Quality"

    def test_strips_a_matrix_suffix(self) -> None:
        # Observed live: the Test job is matrixed over runners.
        assert gate_of("ci / Test (arc-native-16cpu)") == "Test"

    def test_strips_an_unexpanded_matrix_expression(self) -> None:
        # A job that never ran shows the raw expression rather than a value.
        assert gate_of("ci / Build (${{ matrix.os_arch }})") is None

    def test_takes_the_last_segment_when_nested(self) -> None:
        assert gate_of("ci / Release tail / Quality") == "Quality"

    @pytest.mark.parametrize("name", ["Plan", "Commit messages", "Container"])
    def test_non_gate_jobs_are_not_gates(self, name: str) -> None:
        assert gate_of(name) is None


class TestScanFindsTheLastRealExecution:
    """The walk stops at the newest run where a gate produced a verdict."""

    def test_a_gate_that_ran_is_dated_from_its_job(self) -> None:
        runs = [_run(1, 2)]
        jobs = {1: [_job("ci / Quality", "success", 2)]}
        statuses = scan_runs(runs, lambda rid: jobs.get(rid, []))
        assert statuses["Quality"].completed_at == datetime.fromisoformat(
            _ts(2).replace("Z", "+00:00")
        )
        assert statuses["Quality"].skipped_before == 0

    def test_skips_are_walked_past_to_the_last_real_run(self) -> None:
        # The doctrine's shape: green pushes that skipped, an older PR that ran.
        runs = [_run(3, 1), _run(2, 2), _run(1, 30)]
        jobs = {
            3: [_job("ci / Quality", "skipped", 1)],
            2: [_job("ci / Quality", "skipped", 2)],
            1: [_job("ci / Quality", "success", 30)],
        }
        statuses = scan_runs(runs, lambda rid: jobs.get(rid, []))
        assert statuses["Quality"].conclusion == "success"
        assert statuses["Quality"].skipped_before == 2

    def test_a_matrixed_gate_counts_as_executed_if_any_leg_ran(self) -> None:
        runs = [_run(1, 1)]
        jobs = {
            1: [
                _job("ci / Test (arc-native-16cpu)", "success", 1),
                _job("ci / Test (ubuntu-24.04-arm)", "skipped", 1),
            ]
        }
        statuses = scan_runs(runs, lambda rid: jobs.get(rid, []))
        assert statuses["Test"].conclusion == "success"

    def test_a_failing_leg_is_the_decisive_verdict(self) -> None:
        runs = [_run(1, 1)]
        jobs = {
            1: [
                _job("ci / Test (a)", "success", 1),
                _job("ci / Test (b)", "failure", 1),
            ]
        }
        statuses = scan_runs(runs, lambda rid: jobs.get(rid, []))
        assert statuses["Test"].conclusion == "failure"

    @pytest.mark.parametrize("verdict", ["skipped", "cancelled", None])
    def test_no_verdict_conclusions_do_not_count_as_execution(
        self, verdict: str | None
    ) -> None:
        # A cancelled or still-running job answers the question no better than
        # a skipped one.
        runs = [_run(1, 1)]
        jobs = {1: [_job("ci / Quality", verdict, 1)]}
        statuses = scan_runs(runs, lambda rid: jobs.get(rid, []))
        assert statuses["Quality"].completed_at is None

    def test_the_walk_stops_once_every_gate_has_answered(self) -> None:
        # Cost control: a healthy repo must not pay for the whole window.
        runs = [_run(2, 1), _run(1, 2)]
        jobs = {
            2: [_job("ci / Quality", "success", 1), _job("ci / Test", "success", 1)],
            1: [_job("ci / Quality", "success", 2)],
        }
        seen: list[object] = []

        def lookup(run_id: object) -> list[dict]:
            seen.append(run_id)
            return jobs.get(run_id, [])  # type: ignore[arg-type]

        scan_runs(runs, lookup)
        assert seen == [2], "walked past the run that already answered"


class TestAuditReportsOnlyTheInvisibleFault:
    """Stale and never-executed are findings. Failing is not."""

    def _audit(self, runs: list[dict], jobs: dict[int, list[dict]]) -> RepoReport:
        return audit_runs("o/r", runs, lambda rid: jobs.get(rid, []), now=NOW)

    def test_a_recently_executed_gate_is_clean(self) -> None:
        runs = [_run(1, 1)]
        jobs = {
            1: [_job("ci / Quality", "success", 1), _job("ci / Test", "success", 1)]
        }
        assert self._audit(runs, jobs).ok

    def test_a_gate_older_than_the_window_is_stale(self) -> None:
        runs = [_run(1, 30)]
        jobs = {
            1: [_job("ci / Quality", "success", 30), _job("ci / Test", "success", 30)]
        }
        report = self._audit(runs, jobs)
        assert [f.kind for f in report.findings] == ["stale", "stale"]

    def test_a_gate_skipped_in_every_run_never_executed(self) -> None:
        # THE bug: every run concluded success, and nothing was verified.
        runs = [_run(2, 1), _run(1, 2)]
        jobs = {
            2: [_job("ci / Quality", "skipped", 1), _job("ci / Test", "skipped", 1)],
            1: [_job("ci / Quality", "skipped", 2), _job("ci / Test", "skipped", 2)],
        }
        report = self._audit(runs, jobs)
        assert {f.kind for f in report.findings} == {"never"}
        assert not report.ok

    def test_a_failing_gate_is_never_a_finding(self) -> None:
        # A red repo is already visible, and pre-GA repos are meant to be red;
        # reporting it is the noise that gets a reporter ignored.
        runs = [_run(1, 1)]
        jobs = {
            1: [_job("ci / Quality", "failure", 1), _job("ci / Test", "failure", 1)]
        }
        report = self._audit(runs, jobs)
        assert report.ok, "a red repo must not be reported — only an unrun gate"

    def test_a_failing_gate_that_is_also_stale_reports_only_staleness(self) -> None:
        runs = [_run(1, 40)]
        jobs = {
            1: [_job("ci / Quality", "failure", 40), _job("ci / Test", "failure", 40)]
        }
        report = self._audit(runs, jobs)
        assert {f.kind for f in report.findings} == {"stale"}

    def test_a_repo_without_a_gate_job_is_not_accused(self) -> None:
        # No Quality job at all is a different workflow shape, not a lie.
        runs = [_run(1, 1)]
        jobs = {1: [_job("ci / Plan", "success", 1)]}
        report = self._audit(runs, jobs)
        assert report.ok

    def test_a_repo_with_no_runs_yields_no_findings(self) -> None:
        # audit_repo turns this into an error before it gets here; the walk
        # itself must not invent a finding from an empty history.
        report = self._audit([], {})
        assert report.findings == []


class TestFindingText:
    """The report has to say the thing, not hint at it."""

    def test_stale_names_the_age(self) -> None:
        assert "12 days ago" in Finding("stale", "Quality", age_days=12.4).describe()

    def test_never_says_skipped_every_time(self) -> None:
        text = Finding("never", "Quality", runs_scanned=20).describe()
        assert "20 runs" in text
        assert "skipped every time" in text

    def test_a_single_day_reads_as_singular(self) -> None:
        assert "1 day ago" in Finding("stale", "Quality", age_days=1.2).describe()

    def test_a_single_run_reads_as_singular(self) -> None:
        assert "1 run " in Finding("never", "Quality", runs_scanned=1).describe()


class TestPrereleaseIsExempt:
    """A dormant gate is expected pre-GA, so those repos are not accused."""

    @staticmethod
    def _declaring(monkeypatch: pytest.MonkeyPatch, channel: str | None) -> bool:
        monkeypatch.setattr(gate_audit, "repo_channel", lambda _full: channel)
        return gate_audit.is_prerelease("o/r")

    @pytest.mark.parametrize("channel", ["spike", "alpha", "beta"])
    def test_pre_ga_channels_are_exempt(
        self, monkeypatch: pytest.MonkeyPatch, channel: str
    ) -> None:
        assert self._declaring(monkeypatch, channel)

    def test_the_channel_match_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._declaring(monkeypatch, "Beta")

    @pytest.mark.parametrize("channel", ["release", "stable", ""])
    def test_ga_channels_are_audited(
        self, monkeypatch: pytest.MonkeyPatch, channel: str
    ) -> None:
        assert not self._declaring(monkeypatch, channel)

    def test_declaring_nothing_is_treated_as_ga(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Silence must not buy an exemption, or every repo opts out by
        # omission.
        assert not self._declaring(monkeypatch, None)

    def test_the_exempt_set_is_the_pre_ga_channels(self) -> None:
        assert PRERELEASE_CHANNELS == {"spike", "alpha", "beta"}


class TestChannelParsing:
    """`publish.channel` is read from the repo's own config, not guessed."""

    @staticmethod
    def _parse(monkeypatch: pytest.MonkeyPatch, body: str) -> str | None:
        def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout=body)

        monkeypatch.setattr(gate_audit.subprocess, "run", fake_run)
        return gate_audit.repo_channel("o/r")

    def test_reads_a_declared_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._parse(monkeypatch, "publish:\n  channel: beta\n") == "beta"

    def test_a_config_without_publish_declares_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._parse(monkeypatch, "language: rust\n") is None

    def test_publish_without_a_channel_declares_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The shape most repos are in: publish is configured, channel is not.
        assert self._parse(monkeypatch, "publish:\n  enabled: true\n") is None

    def test_unparseable_config_declares_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._parse(monkeypatch, "publish:\n\tchannel: beta\n") is None

    def test_a_missing_config_declares_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout="")

        monkeypatch.setattr(gate_audit.subprocess, "run", fake_run)
        assert gate_audit.repo_channel("o/r") is None


def test_both_doctrine_gates_are_audited() -> None:
    # Quality and Test share one `run-checks` condition, so they go dark
    # together; auditing one and not the other would half-close the hole.
    assert set(GATE_JOBS) == {"Quality", "Test"}
