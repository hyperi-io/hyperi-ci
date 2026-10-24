# Project:   HyperI CI
# File:      tests/unit/test_horizons.py
# Purpose:   `labels --stale`: horizon labels past their release limit
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""dfe-stack reads this report at a cut, so the counting rules and JSON keys are contract."""

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from hyperi_ci import gh, labels_check
from hyperi_ci.cli import app
from hyperi_ci.config import load_config, shipped_default
from hyperi_ci.gh import ApiResult
from hyperi_ci.horizons import (
    CONFIG_KEY,
    HorizonConfigError,
    HorizonReadError,
    ProgramStatus,
    StaleReport,
    Thresholds,
    overlay,
    parse_thresholds,
    report_json,
    stale_report,
)
from hyperi_ci.labels import load_manifest, program_releases_since
from hyperi_ci.labels_check import IssueRef

REPO = "hyperi-io/x"

# HyperI's own set, which declares the dfe program these tests count.
HYPERI_FILE = Path(__file__).resolve().parents[2] / "config" / "labels-hyperi.yaml"
HYPERI = load_manifest(HYPERI_FILE)


def _stale(thresholds: Thresholds) -> StaleReport:
    return stale_report(REPO, thresholds, manifest=HYPERI)


def _issue(number: int, labels: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"issue {number}",
        "created_at": "2026-08-01T00:00:00Z",
        "labels": [{"name": name} for name in labels],
        "user": {"login": "someone", "type": "User"},
        **extra,
    }


def _event(label: str, at: str, kind: str = "labeled") -> dict[str, Any]:
    return {"event": kind, "label": {"name": label}, "created_at": at}


class FakeGitHub:
    """Answers the reads `labels --stale` makes, from plain lists."""

    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.events: dict[int, list[dict[str, Any]]] = {}
        self.tags: list[tuple[str, str]] = []
        # dfe-infra versions.yaml commits, oldest first: (sha, date, latest).
        self.commits: list[tuple[str, str, str]] = []
        self.latest_now = '"2.2.0"'
        self.fail = ""
        self.calls: list[list[str]] = []
        self.refs_read: list[str] = []

    def api(self, args: list[str], *, body: dict | None = None) -> ApiResult:
        self.calls.append(args)
        if args[-1] == "graphql":
            nodes = [{"name": n, "target": {"committedDate": d}} for n, d in self.tags]
            pages = [{"data": {"repository": {"refs": {"nodes": nodes}}}}]
            return ApiResult(pages, "", None)
        endpoint = args[0]
        if self.fail and endpoint.startswith(self.fail):
            return ApiResult(None, f"gh api {endpoint} failed: HTTP 502", "")
        if endpoint.startswith("repos/hyperi-io/.github/commits"):
            missing = "gh: Not Found (HTTP 404)"
            return ApiResult(None, f"gh api {endpoint} failed: {missing}", missing)
        if endpoint.startswith(f"repos/{REPO}/issues?state=open"):
            return ApiResult(self.issues, "", None)
        if match := re.search(r"/issues/(\d+)/events", endpoint):
            return ApiResult(self.events.get(int(match[1]), []), "", None)
        if match := re.search(r"commits\?path=versions\.yaml&until=([^&]+)", endpoint):
            older = [c for c in self.commits if c[1] <= match[1]]
            return ApiResult([self._commit(c) for c in reversed(older)][:1], "", None)
        if match := re.search(r"commits\?path=versions\.yaml&since=([^&]+)", endpoint):
            newer = [c for c in self.commits if c[1] >= match[1]]
            return ApiResult([self._commit(c) for c in reversed(newer)], "", None)
        raise AssertionError(f"unexpected gh api call: {args}")

    @staticmethod
    def _commit(commit: tuple[str, str, str]) -> dict[str, Any]:
        return {"sha": commit[0], "commit": {"committer": {"date": commit[1]}}}

    def read(self, repo: str, path: str, *, ref: str | None = None) -> str | None:
        assert (repo, path) == ("hyperi-io/dfe-infra", "versions.yaml")
        if ref is None:
            latest = self.latest_now
        else:
            self.refs_read.append(ref)
            latest = next(c[2] for c in self.commits if c[0] == ref)
        return f'schema: 2\ncurrent: "9.9.9-rc.1"\nlatest: {latest}\n'

    def endpoints(self, part: str) -> list[list[str]]:
        return [call for call in self.calls if any(part in arg for arg in call)]


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(gh, "gh_api", fake.api)
    monkeypatch.setattr(gh, "read_repo_file", fake.read)
    return fake


def _limits(**project: Any) -> Thresholds:
    raw = overlay(shipped_default(CONFIG_KEY), project or None)
    return parse_thresholds(raw, manifest=HYPERI)


DEFAULTS = _limits()


class TestRepoHorizons:
    def test_past_the_limit_is_stale(self, github: FakeGitHub) -> None:
        github.issues = [_issue(1, ["next-patch"])]
        github.events[1] = [_event("next-patch", "2026-10-01T00:00:00Z")]
        github.tags = [
            ("v1.0.0", "2026-09-30T00:00:00Z"),
            ("v1.0.1", "2026-10-02T00:00:00Z"),
            ("v1.0.2", "2026-10-03T00:00:00Z"),
        ]
        report = _stale(DEFAULTS)
        assert report.checked == 1
        [item] = report.stale
        assert (item.number, item.label, item.scope) == (1, "next-patch", "repo")
        assert (item.releases_since, item.threshold) == (2, 2)
        assert item.label_applied_at == "2026-10-01T00:00:00Z"
        assert item.applied_from == "labeled_event"

    def test_under_the_limit_is_not(self, github: FakeGitHub) -> None:
        github.issues = [_issue(1, ["next-patch"])]
        github.events[1] = [_event("next-patch", "2026-10-01T00:00:00Z")]
        github.tags = [
            ("v1.0.0", "2026-09-30T00:00:00Z"),
            ("v1.0.1", "2026-10-02T00:00:00Z"),
        ]
        report = _stale(DEFAULTS)
        assert report.checked == 1
        assert report.stale == []

    def test_next_major_goes_stale_after_one(self, github: FakeGitHub) -> None:
        github.issues = [_issue(1, ["next-major"])]
        github.events[1] = [_event("next-major", "2026-10-01T00:00:00Z")]
        github.tags = [("v2.0.0", "2026-10-02T00:00:00Z")]
        [item] = _stale(DEFAULTS).stale
        assert (item.releases_since, item.threshold) == (1, 1)

    def test_prerelease_and_non_version_tags_never_count(
        self, github: FakeGitHub
    ) -> None:
        github.issues = [_issue(1, ["next-patch"])]
        github.events[1] = [_event("next-patch", "2026-10-01T00:00:00Z")]
        github.tags = [
            ("v1.1.0-rc.1", "2026-10-02T00:00:00Z"),
            ("v1.1.0-rc.2", "2026-10-03T00:00:00Z"),
            ("v1", "2026-10-04T00:00:00Z"),
            ("1.1.0", "2026-10-05T00:00:00Z"),
        ]
        limits = _limits(stale_after={"next-patch": 1})
        assert _stale(limits).stale == []
        github.tags.append(("v1.1.0", "2026-10-06T00:00:00Z"))
        [item] = _stale(limits).stale
        assert item.releases_since == 1

    def test_a_reapplied_label_counts_from_the_latest_labeled_event(
        self, github: FakeGitHub
    ) -> None:
        github.issues = [_issue(1, ["next-patch"])]
        github.events[1] = [
            _event("next-patch", "2026-09-01T00:00:00Z"),
            _event("next-patch", "2026-09-15T00:00:00Z", "unlabeled"),
            _event("Next-Patch", "2026-10-05T00:00:00Z"),
            _event("backlog", "2026-10-07T00:00:00Z"),
        ]
        github.tags = [
            ("v1.0.1", "2026-09-02T00:00:00Z"),
            ("v1.0.2", "2026-09-03T00:00:00Z"),
            ("v1.0.3", "2026-10-06T00:00:00Z"),
        ]
        assert _stale(DEFAULTS).stale == []
        [item] = _stale(_limits(stale_after={"next-patch": 1})).stale
        assert item.label_applied_at == "2026-10-05T00:00:00Z"
        assert item.releases_since == 1

    def test_no_labeled_event_falls_back_to_issue_creation(
        self, github: FakeGitHub
    ) -> None:
        github.issues = [_issue(1, ["next-patch"])]
        github.tags = [
            ("v1.0.1", "2026-09-02T00:00:00Z"),
            ("v1.0.2", "2026-09-03T00:00:00Z"),
        ]
        [item] = _stale(DEFAULTS).stale
        assert item.applied_from == "issue_created"
        assert item.label_applied_at == "2026-08-01T00:00:00Z"

    def test_release_blocker_and_backlog_are_never_checked(
        self, github: FakeGitHub
    ) -> None:
        github.issues = [_issue(1, ["release-blocker"]), _issue(2, ["backlog"])]
        github.tags = [("v1.0.1", "2026-09-02T00:00:00Z")]
        report = _stale(DEFAULTS)
        assert report.checked == 0
        assert github.endpoints("/events") == []
        assert github.endpoints("graphql") == []

    def test_unreadable_issues_raise(self, github: FakeGitHub) -> None:
        github.fail = f"repos/{REPO}/issues"
        with pytest.raises(HorizonReadError, match="HTTP 502"):
            _stale(DEFAULTS)

    def test_with_no_manifest_the_repos_org_supplies_it(
        self, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HYPERCI_ISSUES_LABELS_PATH")
        load_config(reload=True, report_removed=False)
        github.issues = [_issue(1, ["backlog", "dfe:next-patch"])]
        report = stale_report(REPO, DEFAULTS)
        [lookup] = github.endpoints("/.github/")
        assert lookup[0].startswith("repos/hyperi-io/.github/commits?path=")
        assert report.programs == {}


def _dfe_history(github: FakeGitHub, *after: tuple[str, str]) -> None:
    """A boundary commit at 2.1.0, then one commit per (date, latest)."""
    github.commits = [("c0", "2026-08-01T00:00:00Z", '"2.1.0"')]
    github.commits += [(f"c{i}", d, v) for i, (d, v) in enumerate(after, start=1)]


class TestProgramHorizons:
    def test_counted_from_changes_of_latest(self, github: FakeGitHub) -> None:
        _dfe_history(
            github,
            ("2026-09-05T00:00:00Z", '"2.1.0"'),
            ("2026-09-10T00:00:00Z", '"2.2.0"'),
            ("2026-09-20T00:00:00Z", '"2.2.0"'),
            ("2026-10-01T00:00:00Z", '"2.2.1"'),
        )
        github.issues = [
            _issue(1, ["backlog", "dfe:next-patch"]),
            _issue(2, ["backlog", "dfe:next-patch"]),
        ]
        github.events[1] = [_event("dfe:next-patch", "2026-09-01T00:00:00Z")]
        github.events[2] = [_event("dfe:next-patch", "2026-09-15T00:00:00Z")]
        report = _stale(DEFAULTS)
        assert report.programs == {"dfe": ProgramStatus("released", "2.2.0", "")}
        assert report.checked == 2
        [item] = report.stale
        assert (item.number, item.label, item.scope) == (1, "dfe:next-patch", "program")
        assert (item.releases_since, item.threshold) == (2, 2)
        [since] = github.endpoints("&since=")
        assert "since=2026-09-01T00:00:00Z" in since[0]
        assert github.endpoints("graphql") == []

    def test_the_strictest_program_label_is_the_one_checked(
        self, github: FakeGitHub
    ) -> None:
        _dfe_history(github, ("2026-09-10T00:00:00Z", '"3.0.0"'))
        github.issues = [_issue(1, ["backlog", "dfe:next-major", "dfe:next-minor"])]
        github.events[1] = [_event("dfe:next-minor", "2026-09-01T00:00:00Z")]
        report = _stale(DEFAULTS)
        assert report.checked == 1
        assert report.stale == []

    def test_prereleases_and_reverts_never_count(self, github: FakeGitHub) -> None:
        _dfe_history(
            github,
            ("2026-09-02T00:00:00Z", '"2.2.0-rc.1"'),
            ("2026-09-03T00:00:00Z", '"2.2.0"'),
            ("2026-09-04T00:00:00Z", '"2.1.0"'),
            ("2026-09-05T00:00:00Z", '"2.2.0"'),
        )
        found = program_releases_since("dfe", "2026-09-01T00:00:00Z", manifest=HYPERI)
        assert [(r.version, r.at, r.commit) for r in found] == [
            ("2.2.0", "2026-09-03T00:00:00Z", "c2")
        ]

    def test_the_first_ga_counts(self, github: FakeGitHub) -> None:
        github.commits = [
            ("c0", "2026-08-01T00:00:00Z", '""'),
            ("c1", "2026-09-10T00:00:00Z", '"2.2.0"'),
        ]
        found = program_releases_since("dfe", "2026-09-01T00:00:00Z", manifest=HYPERI)
        assert [r.version for r in found] == ["2.2.0"]

    def test_a_file_created_in_the_window_counts_its_first_value(
        self, github: FakeGitHub
    ) -> None:
        github.commits = [("c1", "2026-09-10T00:00:00Z", '"2.2.0"')]
        found = program_releases_since("dfe", "2026-09-01T00:00:00Z", manifest=HYPERI)
        assert [r.commit for r in found] == ["c1"]

    def test_unchanged_history_reads_only_its_two_ends(
        self, github: FakeGitHub
    ) -> None:
        _dfe_history(
            github,
            *((f"2026-09-{day:02d}T00:00:00Z", '"2.1.0"') for day in range(2, 22)),
        )
        assert (
            program_releases_since("dfe", "2026-09-01T00:00:00Z", manifest=HYPERI) == ()
        )
        assert sorted(github.refs_read) == ["c0", "c20"]

    def test_no_ga_yet_is_a_state_and_nothing_goes_stale(
        self, github: FakeGitHub
    ) -> None:
        github.latest_now = '""'
        github.issues = [_issue(1, ["backlog", "dfe:next-patch"])]
        github.events[1] = [_event("dfe:next-patch", "2026-01-01T00:00:00Z")]
        report = _stale(DEFAULTS)
        assert report.programs == {"dfe": ProgramStatus("none", "", "")}
        assert report.stale == []
        assert report.checked == 0
        assert github.endpoints("/events") == []
        assert github.endpoints("/commits") == []
        doc = report_json(report)
        assert doc["programs"]["dfe"] == {
            "released_state": "none",
            "released": "",
            "error": "",
        }

    def test_an_unreadable_history_is_reported_not_raised(
        self, github: FakeGitHub
    ) -> None:
        github.fail = "repos/hyperi-io/dfe-infra/commits"
        github.issues = [_issue(1, ["backlog", "dfe:next-patch"])]
        github.events[1] = [_event("dfe:next-patch", "2026-09-01T00:00:00Z")]
        report = _stale(DEFAULTS)
        status = report.programs["dfe"]
        assert status.released_state == "error"
        assert "HTTP 502" in status.error
        assert report.stale == []


class TestTriage:
    def test_a_gate_conflict_is_reported_and_the_strictest_is_checked(
        self, github: FakeGitHub
    ) -> None:
        github.issues = [_issue(1, ["backlog", "next-patch"])]
        github.events[1] = [_event("next-patch", "2026-10-01T00:00:00Z")]
        github.tags = [
            ("v1.0.1", "2026-10-02T00:00:00Z"),
            ("v1.0.2", "2026-10-03T00:00:00Z"),
        ]
        report = _stale(DEFAULTS)
        assert report.conflicts == [IssueRef(1, "issue 1", "next-patch, backlog")]
        assert [item.label for item in report.stale] == ["next-patch"]
        assert report_json(report)["conflicts"] == [
            {"number": 1, "title": "issue 1", "labels": ["next-patch", "backlog"]}
        ]

    def test_untriaged_skips_pull_requests_and_the_dashboard(
        self, github: FakeGitHub
    ) -> None:
        dashboard = _issue(3, [], title="Dependency Dashboard")
        dashboard["user"] = {"login": "renovate[bot]", "type": "Bot"}
        github.issues = [
            _issue(1, ["dfe:next-patch"]),
            _issue(2, [], pull_request={}),
            dashboard,
        ]
        github.latest_now = '""'
        report = _stale(DEFAULTS)
        assert report.untriaged == [IssueRef(1, "issue 1")]
        assert report.conflicts == []


class TestThresholds:
    def test_the_shipped_defaults(self) -> None:
        assert dict(DEFAULTS.repo) == {
            "next-patch": 2,
            "next-minor": 2,
            "next-major": 1,
        }
        assert dict(DEFAULTS.programs) == {}
        assert DEFAULTS.of("next-patch", "dfe") == 2
        assert DEFAULTS.of("release-blocker") is None
        assert DEFAULTS.of("backlog") is None

    def test_a_project_overrides_per_horizon_and_per_program(self) -> None:
        limits = _limits(
            stale_after={"next-patch": 3}, programs={"dfe": {"next-minor": 4}}
        )
        assert limits.of("next-patch") == 3
        assert limits.of("next-major") == 1
        assert limits.of("next-minor", "dfe") == 4
        assert limits.of("next-patch", "dfe") == 3

    def test_an_override_changes_the_verdict(self, github: FakeGitHub) -> None:
        github.issues = [_issue(1, ["next-patch"])]
        github.events[1] = [_event("next-patch", "2026-10-01T00:00:00Z")]
        github.tags = [
            ("v1.0.1", "2026-10-02T00:00:00Z"),
            ("v1.0.2", "2026-10-03T00:00:00Z"),
        ]
        assert len(_stale(DEFAULTS).stale) == 1
        assert _stale(_limits(stale_after={"next-patch": 3})).stale == []

    def test_a_program_override_changes_the_verdict(self, github: FakeGitHub) -> None:
        _dfe_history(github, ("2026-09-10T00:00:00Z", '"2.2.0"'))
        github.issues = [_issue(1, ["backlog", "dfe:next-patch"])]
        github.events[1] = [_event("dfe:next-patch", "2026-09-01T00:00:00Z")]
        assert _stale(DEFAULTS).stale == []
        limits = _limits(programs={"dfe": {"next-patch": 1}})
        [item] = _stale(limits).stale
        assert item.threshold == 1

    def test_the_project_config_file_is_merged(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "issues:\n  horizons:\n    stale_after:\n      next-patch: 5\n",
            encoding="utf-8",
        )
        cfg = load_config(reload=True, project_dir=tmp_path)
        limits = parse_thresholds(cfg.setting(CONFIG_KEY), manifest=HYPERI)
        assert limits.of("next-patch") == 5
        assert limits.of("next-minor") == 2

    @pytest.mark.parametrize(
        ("project", "message"),
        [
            ({"stale_after": {"release-blocker": 1}}, "only next-patch"),
            ({"stale_after": {"backlog": 3}}, "only next-patch"),
            ({"stale_after": {"next-patch": 0}}, "1 or more"),
            ({"stale_after": {"next-patch": True}}, "1 or more"),
            ({"stale_after": {"next-patch": "2"}}, "1 or more"),
            ({"stale_after": 2}, "must map a horizon"),
            ({"programs": {"nope": {"next-patch": 1}}}, "no such program"),
            ({"programs": {"dfe": {"backlog": 1}}}, "only next-patch"),
        ],
    )
    def test_a_malformed_block_raises(self, project: dict, message: str) -> None:
        with pytest.raises(HorizonConfigError, match=message):
            _limits(**project)

    def test_a_missing_horizon_raises(self) -> None:
        with pytest.raises(HorizonConfigError, match="no `next-major`"):
            parse_thresholds(
                {"stale_after": {"next-patch": 1, "next-minor": 1}}, manifest=HYPERI
            )

    def test_a_non_mapping_block_raises(self) -> None:
        with pytest.raises(HorizonConfigError, match="must be a mapping"):
            overlay(shipped_default(CONFIG_KEY), 3)


class TestCommand:
    def test_a_named_manifest_declares_the_programs_counted(
        self, github: FakeGitHub
    ) -> None:
        _dfe_history(github, ("2026-09-10T00:00:00Z", '"2.2.0"'))
        github.issues = [_issue(1, ["backlog", "dfe:next-patch"])]
        github.events[1] = [_event("dfe:next-patch", "2026-09-01T00:00:00Z")]
        result = self._run("--json", "--manifest", str(HYPERI_FILE))
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert doc["checked"] == 1
        assert doc["programs"]["dfe"]["released_state"] == "released"

    def test_the_default_manifest_declares_none(self, github: FakeGitHub) -> None:
        github.issues = [_issue(1, ["backlog", "dfe:next-patch"])]
        result = self._run("--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["programs"] == {}

    def _run(self, *args: str, remote_config: str | None = None):
        with (
            patch("hyperi_ci.gh.shutil.which", return_value="/usr/bin/gh"),
            patch.object(labels_check, "read_repo_file", return_value=remote_config),
        ):
            return CliRunner().invoke(app, ["labels", "--stale", "--repo", REPO, *args])

    @pytest.fixture
    def stale_issue(self, github: FakeGitHub) -> FakeGitHub:
        github.issues = [_issue(1, ["next-patch"]), _issue(2, [])]
        github.events[1] = [_event("next-patch", "2026-10-01T00:00:00Z")]
        github.tags = [
            ("v1.0.1", "2026-10-02T00:00:00Z"),
            ("v1.0.2", "2026-10-03T00:00:00Z"),
        ]
        return github

    def test_findings_still_exit_zero(self, stale_issue: FakeGitHub) -> None:
        result = self._run()
        assert result.exit_code == 0, result.output

    def test_json_is_the_only_stdout_and_its_keys_are_pinned(
        self, stale_issue: FakeGitHub
    ) -> None:
        result = self._run("--json")
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert set(doc) == {
            "schema",
            "repo",
            "checked",
            "stale",
            "conflicts",
            "untriaged",
            "programs",
        }
        assert doc["schema"] == 1
        assert doc["repo"] == REPO
        assert doc["stale"] == [
            {
                "number": 1,
                "title": "issue 1",
                "label": "next-patch",
                "scope": "repo",
                "releases_since": 2,
                "threshold": 2,
                "label_applied_at": "2026-10-01T00:00:00Z",
                "applied_from": "labeled_event",
            }
        ]
        assert doc["untriaged"] == [{"number": 2, "title": "issue 2"}]

    def test_the_remote_repo_config_sets_the_limit(
        self, stale_issue: FakeGitHub
    ) -> None:
        config = "issues:\n  horizons:\n    stale_after:\n      next-patch: 3\n"
        result = self._run("--json", remote_config=config)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["stale"] == []

    def test_a_malformed_config_exits_two(self, stale_issue: FakeGitHub) -> None:
        config = "issues:\n  horizons:\n    stale_after:\n      backlog: 1\n"
        assert self._run(remote_config=config).exit_code == 2

    def test_unreadable_github_exits_two(self, github: FakeGitHub) -> None:
        github.fail = f"repos/{REPO}/issues"
        assert self._run().exit_code == 2

    def test_no_gh_exits_two(self) -> None:
        with patch("hyperi_ci.gh.shutil.which", return_value=None):
            result = CliRunner().invoke(app, ["labels", "--stale", "--repo", REPO])
        assert result.exit_code == 2

    def test_check_and_stale_are_separate_modes(self) -> None:
        result = CliRunner().invoke(app, ["labels", "--stale", "--check"])
        assert result.exit_code == 2
