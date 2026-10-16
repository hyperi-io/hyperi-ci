# Project:   HyperI CI
# File:      tests/unit/test_labels.py
# Purpose:   The label manifest, its reader, and `labels --check`
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Three other repos import the manifest, so its shape and the gate rule are contract."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from hyperi_ci import gh, labels_check
from hyperi_ci.cli import app
from hyperi_ci.gh import ApiResult, RepoFileError
from hyperi_ci.labels import (
    MANIFEST_FILE,
    AreasError,
    ManifestError,
    area_description,
    area_label,
    conflicts,
    expected_labels,
    gate_labels,
    gate_of,
    is_allowed,
    load_manifest,
    manifest_json,
    parse_areas,
    retired_description,
    retired_labels,
)
from hyperi_ci.labels_check import Report, check_issues, check_labels


def _write(tmp_path: Path, doc: object) -> Path:
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _shipped_doc() -> dict:
    return yaml.safe_load(MANIFEST_FILE.read_text(encoding="utf-8"))


class TestManifest:
    def test_the_shipped_manifest_loads(self) -> None:
        manifest = load_manifest()
        assert [t.name for t in manifest.types] == ["Bug", "Feature", "Task", "Epic"]
        assert manifest.area_prefix == "area/"
        assert manifest.area_color == "bfdadc"

    def test_gate_is_strictest_first(self) -> None:
        assert gate_labels() == (
            "release-blocker",
            "next-patch",
            "next-minor",
            "backlog",
        )

    def test_backlog_keeps_the_label_pm_creates(self) -> None:
        """The repo projects' auto-add filter and `pm backlog` both rely on it."""
        backlog = next(g for g in load_manifest().gate if g.name == "backlog")
        assert backlog.color == "d4c5f9"
        assert backlog.description == "Captured future work, not scheduled now"

    def test_release_notify_labels_are_bot_owned(self) -> None:
        bots = load_manifest().bot_owned
        assert "release-failure" in bots
        assert "release-commit-back" in bots

    def test_a_retired_label_description(self) -> None:
        assert retired_labels()["blocked"] == "needs-decision or blocked-by"
        assert (
            retired_description("needs-decision or blocked-by")
            == "RETIRED: use needs-decision or blocked-by"
        )

    def test_the_result_is_read_only(self) -> None:
        retired = cast(dict[str, str], load_manifest().retired)
        with pytest.raises(TypeError):
            retired["new"] = "x"

    def test_a_copy_loads_from_a_path(self, tmp_path: Path) -> None:
        manifest = load_manifest(_write(tmp_path, _shipped_doc()))
        assert manifest == load_manifest()


class TestMalformedManifest:
    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda d: d.pop("gate"), "`gate`"),
            (lambda d: d["gate"][0].update(color="red"), "six hex digits"),
            (lambda d: d["gate"][0].pop("description"), "`description`"),
            (lambda d: d["gate"][0].update(description="x" * 101), "100"),
            (lambda d: d.update(retired_description="RETIRED"), "{replacement}"),
            (lambda d: d["bot_owned"].append("backlog"), "more than once"),
            (lambda d: d.update(area="area/"), "`area`"),
            (lambda d: d["allowed"].update(patterns=["initiative:("]), "allowed"),
            (lambda d: d["allowed"].pop("literals"), "allowed.literals"),
        ],
    )
    def test_is_refused_with_a_reason(
        self, tmp_path: Path, mutate: Callable[[dict], object], message: str
    ) -> None:
        doc = _shipped_doc()
        mutate(doc)
        with pytest.raises(ManifestError, match=message.replace("{", r"\{")):
            load_manifest(_write(tmp_path, doc))

    def test_not_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.yaml"
        path.write_text("gate: [", encoding="utf-8")
        with pytest.raises(ManifestError, match="labels.yaml"):
            load_manifest(path)

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError, match="mapping"):
            load_manifest(_write(tmp_path, ["gate"]))


class TestGate:
    def test_no_gate_is_untriaged(self) -> None:
        assert gate_of(["security", "area/auth"]) is None

    def test_the_strictest_wins(self) -> None:
        assert gate_of(["backlog", "release-blocker", "next-patch"]) == (
            "release-blocker"
        )

    def test_case_does_not_matter(self) -> None:
        assert gate_of(["Backlog"]) == "backlog"

    def test_one_gate_is_no_conflict(self) -> None:
        assert conflicts(["next-patch", "security"]) == ()
        assert conflicts([]) == ()

    def test_two_gates_conflict_strictest_first(self) -> None:
        assert conflicts(["backlog", "next-patch"]) == ("next-patch", "backlog")


class TestAreas:
    def test_valid(self) -> None:
        areas = parse_areas({"auth": ["src/auth/"], "e2e-seed": ["tests/e2e/"]})
        assert areas == {"auth": ("src/auth/",), "e2e-seed": ("tests/e2e/",)}

    def test_unset_is_none_declared(self) -> None:
        assert parse_areas(None) == {}
        assert parse_areas({}) == {}

    @pytest.mark.parametrize(
        "raw",
        [
            ["auth"],
            {"Auth": ["src/"]},
            {"area/auth": ["src/"]},
            {"auth_x": ["src/"]},
            {"auth": "src/"},
            {"auth": []},
            {"auth": ["src/", 3]},
            {"auth": [" "]},
        ],
    )
    def test_malformed(self, raw: object) -> None:
        with pytest.raises(AreasError):
            parse_areas(raw)

    def test_an_area_becomes_a_label(self) -> None:
        wanted = expected_labels({"auth": ("src/auth/", "src/orgs/")})
        area = wanted[-1]
        assert area.name == area_label("auth") == "area/auth"
        assert area.color == "bfdadc"
        assert area.description == "src/auth/, src/orgs/"

    def test_a_long_path_list_fits_githubs_limit(self) -> None:
        text = area_description([f"src/module_{n}/" for n in range(20)])
        assert len(text) <= 100
        assert text.endswith("...")

    def test_the_project_config_accepts_areas(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "issues:\n  areas:\n    docs: [docs/]\n", encoding="utf-8"
        )
        result = CliRunner().invoke(app, ["labels", "-C", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "area/docs\tbfdadc\tdocs/" in result.output

    def test_a_malformed_block_stops_the_command(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "issues:\n  areas:\n    Docs: docs/\n", encoding="utf-8"
        )
        result = CliRunner().invoke(app, ["labels", "-C", str(tmp_path)])
        assert result.exit_code == 2


def _label(name: str, color: str, description: str) -> dict:
    return {"name": name, "color": color, "description": description}


def _all_expected(areas: dict | None = None) -> list[dict]:
    return [_label(x.name, x.color, x.description) for x in expected_labels(areas)]


def _issue(number: int, labels: list[str], *, typed: bool = True, **extra) -> dict:
    issue = {
        "number": number,
        "title": f"issue {number}",
        "labels": [{"name": name} for name in labels],
        "type": {"name": "Bug"} if typed else None,
        "user": {"login": "someone", "type": "User"},
    }
    issue.update(extra)
    return issue


class TestCheckLabels:
    def test_a_repo_with_every_label_is_clean(self) -> None:
        report = Report()
        check_labels(_all_expected(), {}, report)
        assert report.clean

    def test_missing(self) -> None:
        present = [x for x in _all_expected() if x["name"] != "next-minor"]
        report = Report()
        check_labels(present, {"docs": ("docs/",)}, report)
        assert report.missing == ["next-minor", "area/docs"]

    def test_drift_in_colour_description_and_case(self) -> None:
        present = _all_expected()
        present[0] = _label("Release-Blocker", "B60205", "blocks")
        report = Report()
        check_labels(present, {}, report)
        assert [(d.field, d.actual, d.expected) for d in report.drift] == [
            ("name", "Release-Blocker", "release-blocker"),
            ("description", "blocks", gate_desc("release-blocker")),
        ]
        assert not report.missing

    def test_extra(self) -> None:
        present = [
            *_all_expected(),
            _label("post-ga", "ededed", "After GA"),
            _label("rc14", "ededed", "RETIRED: use a gate label"),
            _label("released", "ededed", ""),
            _label("good first issue", "7057ff", "Good for newcomers"),
            _label("area/ghost", "bfdadc", ""),
            _label("wip", "ededed", ""),
            _label("initiative:edge-ga", "0e8a16", "anything"),
            _label("dfe-v2", "0e8a16", "anything"),
            _label("initiative:Not_A_Slug", "0e8a16", ""),
        ]
        report = Report()
        check_labels(present, {}, report)
        assert [(e.name, e.hint) for e in report.extra] == [
            (
                "post-ga",
                'retired: set description "RETIRED: use backlog or next-patch"',
            ),
            ("area/ghost", "area not declared in issues.areas"),
            ("wip", ""),
            ("initiative:Not_A_Slug", ""),
        ]
        assert not report.drift


def gate_desc(name: str) -> str:
    return next(g.description for g in load_manifest().gate if g.name == name)


class TestCheckIssues:
    def test_a_triaged_issue_is_clean(self) -> None:
        report = Report()
        check_issues(
            [_issue(1, ["next-patch", "area/auth"])], {"auth": ("a/",)}, report
        )
        assert report.clean

    def test_no_gate(self) -> None:
        report = Report()
        check_issues([_issue(1, ["security"])], {}, report)
        assert [r.number for r in report.no_gate] == [1]
        assert not report.conflict

    def test_conflict_is_not_also_no_gate(self) -> None:
        report = Report()
        check_issues([_issue(2, ["backlog", "release-blocker"])], {}, report)
        assert [(r.number, r.detail) for r in report.conflict] == [
            (2, "release-blocker, backlog")
        ]
        assert not report.no_gate

    def test_no_type(self) -> None:
        report = Report()
        check_issues([_issue(3, ["backlog"], typed=False)], {}, report)
        assert [r.number for r in report.no_type] == [3]

    def test_no_area_only_when_areas_are_declared(self) -> None:
        issue = _issue(4, ["backlog"])
        undeclared = Report()
        check_issues([issue], {}, undeclared)
        assert undeclared.clean
        declared = Report()
        check_issues([issue], {"docs": ("docs/",)}, declared)
        assert [r.number for r in declared.no_area] == [4]

    def test_pull_requests_and_the_dashboard_are_skipped(self) -> None:
        dashboard = _issue(6, [], title="Dependency Dashboard")
        dashboard["user"] = {"login": "renovate[bot]", "type": "Bot"}
        report = Report()
        check_issues([_issue(5, [], pull_request={}), dashboard], {}, report)
        assert report.clean


class TestCommand:
    """`labels --check` against gh mocked at the gh_api boundary."""

    def _run(self, labels: object, issues: object, *args: str):
        answers = [ApiResult(labels, "", None), ApiResult(issues, "", None)]
        with (
            patch("hyperi_ci.gh.shutil.which", return_value="/usr/bin/gh"),
            patch.object(labels_check, "gh_api", side_effect=answers) as api,
            patch.object(labels_check, "read_repo_file", return_value=None),
        ):
            result = CliRunner().invoke(
                app, ["labels", "--check", "--repo", "hyperi-io/x", *args]
            )
        return result, api

    def test_clean_exits_zero(self) -> None:
        result, api = self._run(_all_expected(), [_issue(1, ["next-patch"])])
        assert result.exit_code == 0, result.output
        assert api.call_args_list[0].args[0] == [
            "repos/hyperi-io/x/labels?per_page=100",
            "--paginate",
        ]
        assert api.call_args_list[1].args[0] == [
            "repos/hyperi-io/x/issues?state=open&per_page=100",
            "--paginate",
        ]

    def test_a_finding_exits_one(self) -> None:
        result, _ = self._run(_all_expected(), [_issue(1, [])])
        assert result.exit_code == 1

    def test_an_unreadable_repo_exits_two(self) -> None:
        failed = ApiResult(None, "gh api repos/hyperi-io/x/labels failed: 401", "")
        with (
            patch("hyperi_ci.gh.shutil.which", return_value="/usr/bin/gh"),
            patch.object(labels_check, "gh_api", return_value=failed),
            patch.object(labels_check, "read_repo_file", return_value=None),
        ):
            result = CliRunner().invoke(
                app, ["labels", "--check", "--repo", "hyperi-io/x"]
            )
        assert result.exit_code == 2

    def test_no_gh_exits_two(self) -> None:
        with patch("hyperi_ci.gh.shutil.which", return_value=None):
            result = CliRunner().invoke(
                app, ["labels", "--check", "--repo", "hyperi-io/x"]
            )
        assert result.exit_code == 2

    def test_a_remote_repo_declares_its_own_areas(self) -> None:
        config = "issues:\n  areas:\n    docs: [docs/]\n"
        with patch.object(labels_check, "read_repo_file", return_value=config):
            assert labels_check.remote_areas("hyperi-io/x") == {"docs": ("docs/",)}

    def test_an_unreadable_config_is_an_error_not_no_areas(self) -> None:
        failure = RepoFileError("could not read hyperi-io/x:.hyperi-ci.yaml: HTTP 502")
        with (
            patch.object(labels_check, "read_repo_file", side_effect=failure),
            pytest.raises(AreasError, match="HTTP 502"),
        ):
            labels_check.remote_areas("hyperi-io/x")

    def test_an_unreadable_config_exits_2_from_json(self) -> None:
        failure = RepoFileError("could not read hyperi-io/x:.hyperi-ci.yaml: HTTP 502")
        with (
            patch("hyperi_ci.gh.require_gh", return_value=True),
            patch.object(labels_check, "read_repo_file", side_effect=failure),
        ):
            result = CliRunner().invoke(
                app, ["labels", "--json", "--repo", "hyperi-io/x"]
            )
        assert result.exit_code == 2


class TestReadRepoFile:
    @staticmethod
    def _gh(rc: int, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess([], rc, stdout, stderr)

    def test_404_is_absence(self) -> None:
        done = self._gh(1, stderr="gh: Not Found (HTTP 404)")
        with patch.object(gh, "gh_run", return_value=done):
            assert gh.read_repo_file("o/r", "f") is None

    def test_any_other_failure_raises(self) -> None:
        done = self._gh(1, stderr="gh: Bad Gateway (HTTP 502)")
        with (
            patch.object(gh, "gh_run", return_value=done),
            pytest.raises(RepoFileError, match="HTTP 502"),
        ):
            gh.read_repo_file("o/r", "f")

    def test_repo_file_keeps_none_for_both(self) -> None:
        done = self._gh(1, stderr="gh: Bad Gateway (HTTP 502)")
        with patch.object(gh, "gh_run", return_value=done):
            assert gh.repo_file("o/r", "f") is None


class TestAllowed:
    def test_declared_by_issue_management(self) -> None:
        assert is_allowed("initiative:dfe-ga")
        assert is_allowed("dfe-v1")
        assert is_allowed("dfe-v2")

    def test_never_reconciled(self) -> None:
        names = {label.name for label in expected_labels()}
        assert "dfe-v1" not in names

    @pytest.mark.parametrize(
        "name", ["initiative:", "initiative:DFE", "initiative:a--b", "xinitiative:a"]
    )
    def test_the_pattern_is_a_whole_lower_case_slug(self, name: str) -> None:
        assert not is_allowed(name)


class TestJson:
    """`labels --json` is read by consumers that cannot import hyperi_ci."""

    _SHAPE = {
        "schema": int,
        "types": list,
        "gate": list,
        "waiting": list,
        "flags": list,
        "community": list,
        "bot_owned": list,
        "allowed": dict,
        "area": dict,
        "retired": dict,
        "retired_description": str,
        "areas": dict,
    }

    def test_the_top_level_keys_and_types_are_pinned(self) -> None:
        doc = manifest_json()
        assert set(doc) == set(self._SHAPE)
        for key, kind in self._SHAPE.items():
            assert type(doc[key]) is kind, key
        assert doc["schema"] == 1

    def test_nested_shapes(self) -> None:
        doc = manifest_json({"docs": ("docs/",)})
        assert [g["name"] for g in doc["gate"]] == list(gate_labels())
        assert set(doc["gate"][0]) == {"name", "color", "description"}
        assert set(doc["types"][0]) == {"name", "use_for", "not_for"}
        assert set(doc["allowed"]) == {"patterns", "literals"}
        assert doc["area"] == {"prefix": "area/", "color": "bfdadc"}
        assert doc["areas"] == {"docs": ["docs/"]}

    def test_the_command_prints_json_only(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "issues:\n  areas:\n    docs: [docs/]\n", encoding="utf-8"
        )
        result = CliRunner().invoke(app, ["labels", "--json", "-C", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == manifest_json({"docs": ("docs/",)})

    def test_json_and_check_are_separate_modes(self) -> None:
        result = CliRunner().invoke(app, ["labels", "--json", "--check"])
        assert result.exit_code == 2
