# Project:   HyperI CI
# File:      tests/unit/test_gate_audit.py
# Purpose:   A skipped gate must not read as a passing gate (issue #96)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Gate-audit tests.

The job shapes here are the real ones GitHub returns for a consumer calling a
reusable workflow -- `ci / Quality`, and a matrixed `ci / Test (runner)`. No
repo is contacted: `scan_runs` takes its jobs lookup as an argument precisely
so the walk is testable without a network.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from hyperi_ci import gate_audit
from hyperi_ci.cli import app
from hyperi_ci.gate_audit import (
    GATE_JOBS,
    PRERELEASE_CHANNELS,
    Finding,
    FullTierStatus,
    RepoReport,
    audit_runs,
    gate_of,
    is_full_tier,
    log_full_tier,
    scan_full_tier,
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
        assert report.ok, "a red repo must not be reported -- only an unrun gate"

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

    @pytest.mark.parametrize("channel", ["alpha", "beta"])
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
        assert PRERELEASE_CHANNELS == {"alpha", "beta"}


class TestChannelParsing:
    """The channel is read from the repo's own config, not guessed.

    Both namespaces are tried here: this YAML is fetched raw over `gh api`, so
    the fold `load_config` applies to a local file never runs on it.
    """

    @staticmethod
    def _parse(monkeypatch: pytest.MonkeyPatch, body: str) -> str | None:
        def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout=body)

        monkeypatch.setattr(gate_audit, "gh_run", fake_run)
        return gate_audit.repo_channel("o/r")

    def test_reads_a_declared_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._parse(monkeypatch, "publish:\n  channel: beta\n") == "beta"

    def test_reads_the_canonical_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A repo that has done the rename must not read as declaring nothing,
        # which would audit a pre-GA repo as GA.
        assert self._parse(monkeypatch, "release:\n  channel: beta\n") == "beta"

    def test_the_canonical_namespace_wins_over_the_legacy_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = "publish:\n  channel: alpha\nrelease:\n  channel: beta\n"
        assert self._parse(monkeypatch, body) == "beta"

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

        monkeypatch.setattr(gate_audit, "gh_run", fake_run)
        assert gate_audit.repo_channel("o/r") is None

    def test_the_config_is_fetched_raw_through_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(args)
            assert kwargs == {"check": False}
            return SimpleNamespace(returncode=0, stdout="release:\n  channel: beta\n")

        monkeypatch.setattr(gate_audit, "gh_run", fake_run)
        assert gate_audit.repo_channel("o/r") == "beta"
        assert calls == [
            [
                "api",
                "repos/o/r/contents/.hyperi-ci.yaml",
                "--header",
                "Accept: application/vnd.github.raw+json",
            ]
        ]


class TestGhJson:
    """gh output reaches the audit through the project's gh helper."""

    @staticmethod
    def _decode(
        monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
    ) -> object | None:
        def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
            assert args == ["api", "x"]
            assert kwargs == {"check": False}
            return SimpleNamespace(returncode=returncode, stdout=stdout)

        monkeypatch.setattr(gate_audit, "gh_run", fake_run)
        return gate_audit._gh_json(["api", "x"])

    def test_json_is_decoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._decode(monkeypatch, 0, '{"a": 1}') == {"a": 1}

    def test_a_failed_call_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._decode(monkeypatch, 1, '{"a": 1}') is None

    def test_junk_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._decode(monkeypatch, 0, "not json") is None


def _scheduled(run_id: int, days_ago: float) -> dict:
    return {**_run(run_id, days_ago), "created_at": _ts(days_ago), "event": "schedule"}


class TestIsFullTier:
    """Only the full tier's job counts, never the plain or core Test job."""

    @pytest.mark.parametrize(
        "name",
        [
            "Test (full)",
            "ci / Test (full)",
            "ci / Test (full) (arc-native-16cpu)",
            "ci / Test (full, arc-native-16cpu)",
            "ci / Release tail / Test (full)",
        ],
    )
    def test_full_tier_names_match(self, name: str) -> None:
        assert is_full_tier(name)

    @pytest.mark.parametrize(
        "name",
        [
            "Test",
            "ci / Test",
            "ci / Test (arc-native-16cpu)",
            "ci / Test (core)",
            "ci / Test (core) (arc-native-16cpu)",
            "ci / Test (core, arc-native-16cpu)",
            "ci / Test (fullish)",
            "ci / Test (fullish, arc-native-16cpu)",
            "ci / Quality",
        ],
    )
    def test_other_test_jobs_do_not_match(self, name: str) -> None:
        assert not is_full_tier(name)


class TestScanFullTier:
    """The newest run whose full tier passed, or none."""

    def test_a_recent_pass_is_dated_from_its_job(self) -> None:
        runs = [_run(1, 2)]
        jobs = {1: [_job("ci / Test (full) (arc-native-4cpu)", "success", 2)]}
        status = scan_full_tier(runs, lambda rid: jobs.get(rid, []))
        assert status.completed_at == datetime.fromisoformat(
            _ts(2).replace("Z", "+00:00")
        )
        assert status.run_url == "https://github.com/o/r/actions/runs/1"

    def test_no_full_job_anywhere_is_never(self) -> None:
        # Every repo today: the tier exists in no workflow yet.
        runs = [_run(2, 1), _run(1, 2)]
        jobs = {
            2: [_job("ci / Test (arc-native-4cpu)", "success", 1)],
            1: [_job("ci / Test", "success", 2)],
        }
        status = scan_full_tier(runs, lambda rid: jobs.get(rid, []))
        assert status.completed_at is None
        assert status.runs_scanned == 2

    def test_a_failed_full_run_is_walked_past(self) -> None:
        runs = [_run(2, 1), _run(1, 5)]
        jobs = {
            2: [_job("ci / Test (full)", "failure", 1)],
            1: [_job("ci / Test (full)", "success", 5)],
        }
        status = scan_full_tier(runs, lambda rid: jobs.get(rid, []))
        assert status.run_url == "https://github.com/o/r/actions/runs/1"

    def test_one_failing_leg_fails_the_tier(self) -> None:
        runs = [_run(1, 1)]
        jobs = {
            1: [
                _job("ci / Test (full) (a)", "success", 1),
                _job("ci / Test (full) (b)", "failure", 1),
            ]
        }
        assert scan_full_tier(runs, lambda rid: jobs.get(rid, [])).completed_at is None

    @pytest.mark.parametrize("verdict", ["skipped", "cancelled", None])
    def test_a_no_verdict_full_job_is_not_a_pass(self, verdict: str | None) -> None:
        runs = [_run(1, 1)]
        jobs = {1: [_job("ci / Test (full)", verdict, 1)]}
        assert scan_full_tier(runs, lambda rid: jobs.get(rid, [])).completed_at is None

    def test_merged_lists_walk_newest_first_and_once(self) -> None:
        # A nightly from 3 days ago sits behind the recent PR runs; the
        # scheduled listing brings it in, and a duplicate is fetched once.
        recent = [_run(9, 0.1), _run(8, 0.2)]
        nightly = [_scheduled(5, 3), _scheduled(4, 4), _scheduled(9, 0.1)]
        jobs = {
            9: [_job("ci / Test (core)", "success", 0.1)],
            8: [_job("ci / Test (core)", "success", 0.2)],
            5: [_job("ci / Test (full)", "success", 3)],
            4: [_job("ci / Test (full)", "success", 4)],
        }
        seen: list[object] = []

        def lookup(run_id: object) -> list[dict]:
            seen.append(run_id)
            return jobs.get(run_id, [])  # type: ignore[arg-type]

        status = scan_full_tier([*recent, *nightly], lookup)
        assert status.run_url == "https://github.com/o/r/actions/runs/5"
        assert seen == [9, 8, 5]

    def test_pull_request_runs_are_not_fetched(self) -> None:
        # The full tier never runs on a pull request, so its jobs cost a call
        # for nothing.
        runs = [
            {**_run(3, 0.1), "event": "pull_request"},
            {**_run(2, 0.2), "event": "pull_request_target"},
            {**_run(1, 1), "event": "push"},
        ]
        jobs = {1: [_job("ci / Test (full, arc-native-16cpu)", "success", 1)]}
        seen: list[object] = []

        def lookup(run_id: object) -> list[dict]:
            seen.append(run_id)
            return jobs.get(run_id, [])  # type: ignore[arg-type]

        status = scan_full_tier(runs, lookup)
        assert seen == [1]
        assert status.runs_scanned == 1
        assert status.run_url == "https://github.com/o/r/actions/runs/1"

    def test_the_walk_stops_after_the_limit(self) -> None:
        runs = [_run(run_id, run_id) for run_id in range(1, 11)]
        seen: list[object] = []

        def lookup(run_id: object) -> list[dict]:
            seen.append(run_id)
            return []

        status = scan_full_tier(runs, lookup, limit=3)
        assert seen == [1, 2, 3]
        assert status.runs_scanned == 3
        assert status.completed_at is None


class TestGateScanIgnoresSchedules:
    """A nightly run says nothing about whether the gate checked a merge."""

    def test_a_scheduled_run_does_not_answer_for_the_gate(self) -> None:
        runs = [_scheduled(2, 1), _run(1, 30)]
        jobs = {
            2: [
                _job("ci / Quality", "success", 1),
                _job("ci / Test (full)", "success", 1),
            ],
            1: [_job("ci / Quality", "success", 30), _job("ci / Test", "success", 30)],
        }
        seen: list[object] = []

        def lookup(run_id: object) -> list[dict]:
            seen.append(run_id)
            return jobs.get(run_id, [])  # type: ignore[arg-type]

        statuses = scan_runs(runs, lookup)
        assert seen == [1]
        assert statuses["Quality"].run_url == "https://github.com/o/r/actions/runs/1"

    def test_only_scheduled_runs_leave_the_gate_stale(self) -> None:
        runs = [_scheduled(2, 1), _run(1, 30)]
        jobs = {
            2: [_job("ci / Quality", "success", 1), _job("ci / Test", "success", 1)],
            1: [_job("ci / Quality", "success", 30), _job("ci / Test", "success", 30)],
        }
        report = audit_runs("o/r", runs, lambda rid: jobs.get(rid, []), now=NOW)
        assert [f.kind for f in report.findings] == ["stale", "stale"]

    def test_the_full_scan_still_reads_the_scheduled_run(self) -> None:
        runs = [_scheduled(2, 1), _run(1, 2)]
        jobs = {
            2: [_job("ci / Test (full)", "success", 1)],
            1: [_job("ci / Quality", "success", 2), _job("ci / Test", "success", 2)],
        }
        report = audit_runs("o/r", runs, lambda rid: jobs.get(rid, []), now=NOW)
        assert report.ok
        assert report.full_tier is not None
        assert report.full_tier.run_url == "https://github.com/o/r/actions/runs/2"


class TestFullTierExpected:
    """Which repos owe a full run: a schedule trigger or the release opt-in."""

    @pytest.mark.parametrize(
        "body",
        [
            "on:\n  schedule:\n    - cron: '0 3 * * *'\n  push:\n",
            "on: [push, schedule]\n",
            "on: schedule\n",
            "'on':\n  schedule:\n    - cron: '0 3 * * *'\n",
        ],
    )
    def test_schedule_triggers_are_read(self, body: str) -> None:
        assert gate_audit.triggers_on_schedule(yaml.safe_load(body))

    @pytest.mark.parametrize(
        "body",
        [
            "on:\n  push:\n  pull_request:\n",
            "on: [push]\n",
            "on: push\n",
            "name: CI\n",
        ],
    )
    def test_other_triggers_are_not_a_schedule(self, body: str) -> None:
        assert not gate_audit.triggers_on_schedule(yaml.safe_load(body))

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({"test": {"full": {"required_for_release": True}}}, True),
            ({"test": {"full": {"required_for_release": "yes"}}}, True),
            ({"test": {"full": {"required_for_release": False}}}, False),
            ({"test": {"full": {}}}, False),
            ({"test": "nonsense"}, False),
            ({}, False),
            (None, False),
        ],
    )
    def test_the_release_opt_in_is_read(
        self, config: dict | None, expected: bool
    ) -> None:
        assert gate_audit.requires_full_for_release(config) is expected

    @staticmethod
    def _expects(monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> bool:
        def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
            path = args[1].split("/contents/", 1)[1]
            if path in files:
                return SimpleNamespace(returncode=0, stdout=files[path])
            return SimpleNamespace(returncode=1, stdout="")

        monkeypatch.setattr(gate_audit, "gh_run", fake_run)
        return gate_audit.expects_full_tier("o/r")

    def test_a_scheduled_caller_expects_full(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        caller = "on:\n  schedule:\n    - cron: '0 3 * * *'\n"
        assert self._expects(monkeypatch, {".github/workflows/ci.yml": caller})

    def test_the_release_opt_in_expects_full(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        files = {
            ".github/workflows/ci.yml": "on: [push]\n",
            ".hyperi-ci.yaml": "test:\n  full:\n    required_for_release: true\n",
        }
        assert self._expects(monkeypatch, files)

    def test_neither_expects_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        files = {".github/workflows/ci.yml": "on: [push]\n", ".hyperi-ci.yaml": ""}
        assert not self._expects(monkeypatch, files)

    def test_unreadable_files_expect_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert not self._expects(monkeypatch, {})


class TestFullTierReport:
    """A date or "never" per repo, flagged when stale, never a finding."""

    def _audit(
        self,
        jobs: dict[int, list[dict]],
        runs: list[dict],
        scheduled: list[dict] | None = None,
        *,
        expected: bool = True,
    ) -> RepoReport:
        return audit_runs(
            "o/r",
            runs,
            lambda rid: jobs.get(rid, []),
            now=NOW,
            scheduled_runs=scheduled,
            full_tier_expected=expected,
        )

    @staticmethod
    def _gates_ran(days_ago: float, test_name: str) -> list[dict]:
        return [
            _job("ci / Quality", "success", days_ago),
            _job(test_name, "success", days_ago),
        ]

    def test_a_recent_full_pass_lists_its_date(self) -> None:
        report = self._audit({1: self._gates_ran(2, "ci / Test (full)")}, [_run(1, 2)])
        assert report.full_tier is not None
        text = report.full_tier.describe(now=NOW, max_age_days=7)
        assert text == "Test (full) last passed: 2026-08-03 (2 days ago)"
        assert not report.full_tier.is_stale(now=NOW, max_age_days=7)

    def test_an_old_full_pass_is_flagged_stale(self) -> None:
        jobs = {
            2: self._gates_ran(1, "ci / Test (core)"),
            1: [_job("ci / Test (full)", "success", 30)],
        }
        report = self._audit(jobs, [_run(2, 1)], scheduled=[_scheduled(1, 30)])
        assert report.full_tier is not None
        text = report.full_tier.describe(now=NOW, max_age_days=7)
        assert text == (
            "Test (full) last passed: 2026-07-06 (30 days ago)"
            " -- STALE, older than 7 days"
        )

    def test_no_full_pass_reads_never_and_counts_as_stale(self) -> None:
        report = self._audit({1: self._gates_ran(1, "ci / Test")}, [_run(1, 1)])
        assert report.full_tier is not None
        assert report.full_tier.is_stale(now=NOW, max_age_days=7)
        assert report.full_tier.describe(now=NOW, max_age_days=7) == (
            "Test (full) last passed: never (none in 1 run scanned)"
            " -- STALE, older than 7 days"
        )

    def test_a_repo_not_expecting_full_is_not_called_stale(self) -> None:
        report = self._audit(
            {1: self._gates_ran(1, "ci / Test")}, [_run(1, 1)], expected=False
        )
        assert report.full_tier is not None
        assert report.full_tier.describe(now=NOW, max_age_days=7) == (
            "Test (full) last passed: never (none in 1 run scanned)"
            " -- not expected: no schedule, full not required to release"
        )

    def test_the_window_is_the_audit_window(self) -> None:
        status = FullTierStatus(completed_at=NOW - timedelta(days=10))
        assert status.is_stale(now=NOW, max_age_days=7)
        assert not status.is_stale(now=NOW, max_age_days=14)

    def test_a_stale_or_missing_full_tier_is_not_a_finding(self) -> None:
        # The tier ships before any repo runs it; a red audit on that alone
        # would be noise.
        report = self._audit({1: self._gates_ran(1, "ci / Test")}, [_run(1, 1)])
        assert report.findings == []
        assert report.ok

    def test_the_gate_scan_and_the_full_scan_share_fetches(self) -> None:
        seen: list[object] = []
        jobs = {1: self._gates_ran(1, "ci / Test")}

        def lookup(run_id: object) -> list[dict]:
            seen.append(run_id)
            return jobs.get(run_id, [])  # type: ignore[arg-type]

        audit_runs("o/r", [_run(1, 1)], lookup, now=NOW)
        assert seen == [1]

    def test_only_an_expected_stale_tier_logs_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infos: list[str] = []
        warnings: list[str] = []
        monkeypatch.setattr(gate_audit, "info", infos.append)
        monkeypatch.setattr(gate_audit, "warn", warnings.append)

        fresh = FullTierStatus(NOW - timedelta(1), expected=True)
        owed = FullTierStatus(runs_scanned=20, expected=True)
        unowed = FullTierStatus(runs_scanned=20, expected=False)
        for repo, status in (("o/fresh", fresh), ("o/owed", owed), ("o/un", unowed)):
            log_full_tier(RepoReport(repo, full_tier=status), max_age_days=7, now=NOW)

        assert infos == [
            "o/fresh: Test (full) last passed: 2026-08-04 (1 day ago)",
            "o/un: Test (full) last passed: never (none in 20 runs scanned)"
            " -- not expected: no schedule, full not required to release",
        ]
        assert warnings == [
            "o/owed: Test (full) last passed: never (none in 20 runs scanned)"
            " -- STALE, older than 7 days"
        ]

    def test_a_report_without_a_full_tier_logs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lines: list[str] = []
        monkeypatch.setattr(gate_audit, "info", lines.append)
        monkeypatch.setattr(gate_audit, "warn", lines.append)
        log_full_tier(RepoReport("o/r", error="no ci.yml"), max_age_days=7, now=NOW)
        assert lines == []


_SCHEDULED_CALLER = "on:\n  schedule:\n    - cron: '0 3 * * *'\n  push:\n"
_PUSH_CALLER = "on:\n  push:\n  pull_request:\n"


class TestAuditGatesCommand:
    """End to end through `hyperi-ci audit-gates`, with gh faked."""

    @staticmethod
    def _fake_gh(
        monkeypatch: pytest.MonkeyPatch,
        full_days_ago: float | None,
        caller: str,
    ) -> list[str]:
        now = datetime.now(UTC)

        def ts(days_ago: float) -> str:
            return (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

        recent = {
            "id": 2,
            "event": "push",
            "created_at": ts(0.5),
            "updated_at": ts(0.5),
        }
        nightly = {
            "id": 1,
            "event": "schedule",
            "created_at": ts(3),
            "updated_at": ts(3),
        }
        gates = [
            {"name": "ci / Quality", "conclusion": "success", "completed_at": ts(0.5)},
            {
                "name": "ci / Test (core)",
                "conclusion": "success",
                "completed_at": ts(0.5),
            },
        ]
        full = (
            []
            if full_days_ago is None
            else [
                {
                    "name": "ci / Test (full) (arc-native-16cpu)",
                    "conclusion": "success",
                    "completed_at": ts(full_days_ago),
                }
            ]
        )
        calls: list[str] = []

        def fake(args: list[str], **_kwargs: object) -> SimpleNamespace:
            path = args[1]
            calls.append(path)
            if path.endswith("/contents/.github/workflows/ci.yml"):
                return SimpleNamespace(returncode=0, stdout=caller)
            if "/contents/" in path:
                return SimpleNamespace(returncode=1, stdout="")
            if "/jobs" in path:
                body: object = {"jobs": gates if "/runs/2/" in path else full}
            elif "event=schedule" in path:
                body = {"workflow_runs": [nightly]}
            else:
                body = {"workflow_runs": [recent]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body))

        monkeypatch.setattr(gate_audit, "gh_run", fake)
        return calls

    @pytest.mark.parametrize(
        ("full_days_ago", "caller", "level", "marker"),
        [
            (3, _SCHEDULED_CALLER, "info", None),
            (30, _SCHEDULED_CALLER, "warn", "STALE"),
            (None, _SCHEDULED_CALLER, "warn", "STALE"),
            (None, _PUSH_CALLER, "info", "not expected"),
        ],
    )
    def test_the_exit_status_ignores_the_full_tier(
        self,
        monkeypatch: pytest.MonkeyPatch,
        full_days_ago: float | None,
        caller: str,
        level: str,
        marker: str | None,
    ) -> None:
        logged: dict[str, list[str]] = {"info": [], "warn": []}
        monkeypatch.setattr(gate_audit, "info", logged["info"].append)
        monkeypatch.setattr(gate_audit, "warn", logged["warn"].append)
        calls = self._fake_gh(monkeypatch, full_days_ago, caller)

        result = CliRunner().invoke(app, ["audit-gates", "--repo", "o/r"])

        assert result.exit_code == 0, result.output
        assert any("event=schedule" in call for call in calls)
        lines = logged[level]
        assert len(lines) == 1
        assert sum(len(v) for v in logged.values()) == 1
        assert lines[0].startswith("o/r: Test (full) last passed: ")
        if full_days_ago is None:
            assert "never" in lines[0]
        if marker is None:
            assert " -- " not in lines[0]
        else:
            assert marker in lines[0]


def test_both_doctrine_gates_are_audited() -> None:
    # Quality and Test share one `run-checks` condition, so they go dark
    # together; auditing one and not the other would half-close the hole.
    assert set(GATE_JOBS) == {"Quality", "Test"}
