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

from hyperi_ci import common, gh, labels_check
from hyperi_ci.cli import app
from hyperi_ci.common import run_cmd
from hyperi_ci.config import load_config
from hyperi_ci.gh import ApiResult, RepoFileError
from hyperi_ci.labels import (
    MANIFEST_FILE,
    AreasError,
    Manifest,
    ManifestError,
    ManifestSource,
    NoReleaseYetError,
    ProgramError,
    area_description,
    area_label,
    conflicts,
    expected_labels,
    gate_labels,
    gate_of,
    horizon_line,
    is_allowed,
    load_manifest,
    manifest_json,
    parse_areas,
    program_members,
    program_released,
    program_version,
    resolve_org,
    resolve_program_horizon,
    retired_description,
    retired_labels,
)
from hyperi_ci.labels_check import Report, check_issues, check_labels

# HyperI's own set, kept in the repo to seed hyperi-io/.github.
HYPERI_FILE = Path(__file__).resolve().parents[2] / "config" / "labels-hyperi.yaml"
HYPERI = load_manifest(HYPERI_FILE)


def _write(tmp_path: Path, doc: object) -> Path:
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _hyperi_doc() -> dict:
    return yaml.safe_load(HYPERI_FILE.read_text(encoding="utf-8"))


class TestManifest:
    def test_the_shipped_manifest_loads(self) -> None:
        manifest = load_manifest()
        assert [t.name for t in manifest.types] == ["Bug", "Feature", "Task", "Epic"]
        assert manifest.area_prefix == "area/"
        assert manifest.area_color == "bfdadc"
        assert manifest.source == ManifestSource(
            "packaged-default", path="hyperi_ci/config/labels.yaml"
        )

    def test_gate_is_strictest_first(self) -> None:
        assert gate_labels() == (
            "release-blocker",
            "next-patch",
            "next-minor",
            "next-major",
            "backlog",
        )

    def test_next_major_sits_between_next_minor_and_backlog(self) -> None:
        assert gate_of(["backlog", "next-major"]) == "next-major"
        assert gate_of(["next-major", "next-minor"]) == "next-minor"

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
        retired = retired_labels(manifest=HYPERI)
        assert retired["blocked"] == "needs-decision or blocked-by"
        assert (
            retired_description("needs-decision or blocked-by", manifest=HYPERI)
            == "RETIRED: use needs-decision or blocked-by"
        )

    def test_the_result_is_read_only(self) -> None:
        retired = cast(dict[str, str], HYPERI.retired)
        with pytest.raises(TypeError):
            retired["new"] = "x"

    def test_a_copy_loads_from_a_path(self, tmp_path: Path) -> None:
        shipped = yaml.safe_load(MANIFEST_FILE.read_text(encoding="utf-8"))
        path = _write(tmp_path, shipped)
        manifest = load_manifest(path)
        assert manifest == load_manifest()
        assert manifest.source == ManifestSource("file", path=str(path))


class TestPackagedDefault:
    """The shipped default is neutral, and HyperI's set lives beside the code."""

    def test_it_carries_nothing_of_one_org(self) -> None:
        packaged = load_manifest(MANIFEST_FILE)
        assert dict(packaged.programs) == {}
        assert dict(packaged.retired) == {}
        assert packaged.allowed_patterns == ()
        assert packaged.allowed_literals == ()
        content = json.dumps(yaml.safe_load(MANIFEST_FILE.read_text(encoding="utf-8")))
        assert "dfe" not in content
        assert "hyperi" not in content.casefold()

    def test_it_still_carries_every_horizon(self) -> None:
        names = gate_labels(manifest=load_manifest(MANIFEST_FILE))
        assert names == (
            "release-blocker",
            "next-patch",
            "next-minor",
            "next-major",
            "backlog",
        )

    def test_the_hyperi_set_is_the_default_plus_its_own(self) -> None:
        packaged = load_manifest(MANIFEST_FILE)
        for section in ("gate", "horizons", "waiting", "flags", "community"):
            assert getattr(HYPERI, section) == getattr(packaged, section), section
        assert [t.name for t in HYPERI.types] == [t.name for t in packaged.types]
        assert set(packaged.bot_owned) < set(HYPERI.bot_owned)
        assert (HYPERI.area_prefix, HYPERI.area_color) == (
            packaged.area_prefix,
            packaged.area_color,
        )
        assert HYPERI.retired_template == packaged.retired_template
        assert list(HYPERI.programs) == ["dfe"]
        assert HYPERI.retired["post-ga"] == "backlog or next-patch"
        assert HYPERI.allowed_literals == ("dfe-v1", "dfe-v2")
        assert HYPERI.allowed_patterns == ("initiative:[a-z0-9]+(?:-[a-z0-9]+)*",)

    def test_both_declare_the_schema(self) -> None:
        for path in (MANIFEST_FILE, HYPERI_FILE):
            assert yaml.safe_load(path.read_text(encoding="utf-8"))["schema"] == 1


class TestSchema:
    def test_a_missing_schema_is_version_one(self, tmp_path: Path) -> None:
        doc = _hyperi_doc()
        del doc["schema"]
        assert load_manifest(_write(tmp_path, doc)) == HYPERI

    @pytest.mark.parametrize("schema", [2, 0, "1", True, None])
    def test_any_other_schema_is_refused(self, tmp_path: Path, schema: object) -> None:
        doc = _hyperi_doc()
        doc["schema"] = schema
        with pytest.raises(ManifestError, match="`schema`"):
            load_manifest(_write(tmp_path, doc))


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
            (lambda d: d["gate"][3].pop("set_by"), "set_by"),
            (lambda d: d["gate"].pop(3), "next-major"),
            (lambda d: d.pop("programs"), "`programs`"),
            (
                lambda d: d["programs"]["dfe"]["labels"].update(backlog="x"),
                "'backlog' is not one of",
            ),
            (
                lambda d: d["programs"]["dfe"]["labels"].update(
                    {"next-patch": "x" * 101}
                ),
                "100",
            ),
            (
                lambda d: d["programs"]["dfe"]["members"].update(repo="dfe-infra"),
                "owner/name",
            ),
            (lambda d: d["programs"]["dfe"].pop("version"), "programs.dfe.version"),
            (lambda d: d["programs"]["dfe"].pop("released"), "programs.dfe.released"),
            (
                lambda d: d["retired"].update({"dfe:next-patch": "x"}),
                "more than once",
            ),
        ],
    )
    def test_is_refused_with_a_reason(
        self, tmp_path: Path, mutate: Callable[[dict], object], message: str
    ) -> None:
        doc = _hyperi_doc()
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


def _all_expected(
    areas: dict | None = None, manifest: Manifest | None = None
) -> list[dict]:
    wanted = expected_labels(areas, manifest=manifest)
    return [_label(x.name, x.color, x.description) for x in wanted]


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
            *_all_expected(manifest=HYPERI),
            _label("post-ga", "ededed", "After GA"),
            _label("rc14", "ededed", "RETIRED: use a gate label"),
            _label("released", "ededed", ""),
            _label("github_actions", "000000", ""),
            _label("good first issue", "7057ff", "Good for newcomers"),
            _label("area/ghost", "bfdadc", ""),
            _label("wip", "ededed", ""),
            _label("initiative:edge-ga", "0e8a16", "anything"),
            _label("dfe-v2", "0e8a16", "anything"),
            _label("initiative:Not_A_Slug", "0e8a16", ""),
        ]
        report = Report()
        check_labels(present, {}, report, manifest=HYPERI)
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

    def test_the_default_has_no_opinion_on_another_orgs_labels(self) -> None:
        present = [*_all_expected(), _label("post-ga", "ededed", "After GA")]
        report = Report()
        check_labels(present, {}, report)
        assert [(e.name, e.hint) for e in report.extra] == [("post-ga", "")]


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

    def test_retired_label_on_an_issue(self) -> None:
        report = Report()
        issue = _issue(7, ["next-patch", "post-ga", "Bug"])
        check_issues([issue], {}, report, manifest=HYPERI)
        assert [(r.number, r.detail) for r in report.retired] == [
            (7, "post-ga -> backlog or next-patch, Bug -> type Bug")
        ]
        assert not report.no_gate

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
        assert is_allowed("initiative:dfe-ga", manifest=HYPERI)
        assert is_allowed("dfe-v1", manifest=HYPERI)
        assert is_allowed("dfe-v2", manifest=HYPERI)

    def test_the_default_allows_nothing(self) -> None:
        assert not is_allowed("initiative:dfe-ga")
        assert not is_allowed("dfe-v1")

    def test_never_reconciled(self) -> None:
        names = {label.name for label in expected_labels(manifest=HYPERI)}
        assert "dfe-v1" not in names

    @pytest.mark.parametrize(
        "name", ["initiative:", "initiative:DFE", "initiative:a--b", "xinitiative:a"]
    )
    def test_the_pattern_is_a_whole_lower_case_slug(self, name: str) -> None:
        assert not is_allowed(name, manifest=HYPERI)


# Excerpts of dfe-infra's suite.yaml and versions.yaml, in their real shape.
_SUITE = """\
schema: "1"
verified: "2026-09-03"
nodes:
  dfe-infra:
    repo: hyperi-io/dfe-infra
    role: infra
    maturity: release
  dfe-loader:
    repo: hyperi-io/dfe-loader
    role: service
    artefacts:
      - kind: container
        registry: ghcr
        public: false
  scalo-rs:
    repo: hyperi-io/scalo-rs
    package: scalo
    role: library
edges:
  - from: scalo-rs
    to: dfe-loader
    kind: cargo-dep
"""

_VERSIONS = """\
schema: 2
current: "{current}"
latest: "{latest}"
lts:
stacks:
  2.2.0:
    maturity: release
    support: standard
  2.2.1-rc.17:
    maturity: rc
    support: standard
"""


@pytest.fixture
def dfe_infra(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    """Serve dfe-infra's files from a dict, editable per test."""
    files: dict[str, str | None] = {
        "suite.yaml": _SUITE,
        "versions.yaml": _VERSIONS.format(current="2.2.1-rc.17", latest="2.2.0"),
    }

    def read(repo: str, path: str) -> str | None:
        assert repo == "hyperi-io/dfe-infra"
        return files[path]

    monkeypatch.setattr(gh, "read_repo_file", read)
    return files


class TestHorizonLine:
    @pytest.mark.parametrize(
        ("version", "patch_line", "minor_line", "major_line"),
        [
            ("2.2.1-rc.17", "2.2.1", "2.3", "3.0"),
            ("2.2.0", "2.2.1", "2.3", "3.0"),
            ("2.2.1", "2.2.2", "2.3", "3.0"),
            ("2.3.0-rc.1", "2.3.1", "2.3", "3.0"),
            ("3.0.0-rc.1", "3.0.1", "3.1", "3.0"),
            ("3.0.0", "3.0.1", "3.1", "4.0"),
            ("0.9.3", "0.9.4", "0.10", "1.0"),
            ("v2.9.10", "2.9.11", "2.10", "3.0"),
            ("2.2.1+build.5", "2.2.2", "2.3", "3.0"),
            ("2.2.1-rc.1+build.5", "2.2.1", "2.3", "3.0"),
        ],
    )
    def test_each_horizon(
        self, version: str, patch_line: str, minor_line: str, major_line: str
    ) -> None:
        assert horizon_line(version, "next-patch") == patch_line
        assert horizon_line(version, "next-minor") == minor_line
        assert horizon_line(version, "next-major") == major_line

    @pytest.mark.parametrize("version", ["2.2", "latest", "", "2.2.1.4", "2.x.1"])
    def test_a_non_semver_version_raises(self, version: str) -> None:
        with pytest.raises(ProgramError, match="MAJOR.MINOR.PATCH"):
            horizon_line(version, "next-patch")

    @pytest.mark.parametrize("horizon", ["release-blocker", "backlog", "next"])
    def test_only_next_horizons_have_a_line(self, horizon: str) -> None:
        with pytest.raises(ProgramError, match="no release line"):
            horizon_line("2.2.0", horizon)


class TestPrograms:
    def test_expected_labels_without_programs_is_unchanged(self) -> None:
        names = [label.name for label in expected_labels({"docs": ("docs/",)})]
        assert names == [
            *gate_labels(),
            "needs-decision",
            "security",
            "regression",
            "area/docs",
        ]

    def test_expected_labels_adds_the_program_family(self) -> None:
        areas = {"docs": ("docs/",)}
        wanted = expected_labels(areas, programs=["dfe"], manifest=HYPERI)
        names = [label.name for label in wanted]
        assert names[-4:] == [
            "dfe:next-patch",
            "dfe:next-minor",
            "dfe:next-major",
            "area/docs",
        ]
        family = {label.name: label for label in wanted}
        gate = {label.name: label for label in HYPERI.gate}
        assert family["dfe:next-major"].color == gate["next-major"].color

    def test_an_unknown_program_raises(self) -> None:
        with pytest.raises(ProgramError, match="'nope'"):
            expected_labels(programs=["nope"], manifest=HYPERI)

    def test_the_default_declares_no_program(self) -> None:
        with pytest.raises(ProgramError, match="'dfe'"):
            expected_labels(programs=["dfe"])

    def test_no_program_has_release_blocker_or_backlog(self) -> None:
        for program in HYPERI.programs.values():
            assert program.horizons == ("next-patch", "next-minor", "next-major")

    def test_members_are_every_node_repo(self, dfe_infra: dict) -> None:
        assert program_members("dfe", manifest=HYPERI) == {
            "hyperi-io/dfe-infra",
            "hyperi-io/dfe-loader",
            "hyperi-io/scalo-rs",
        }

    def test_resolve_each_horizon(self, dfe_infra: dict) -> None:
        assert program_version("dfe", manifest=HYPERI) == "2.2.1-rc.17"
        assert resolve_program_horizon("dfe", "next-patch", manifest=HYPERI) == "2.2.1"
        assert resolve_program_horizon("dfe", "next-minor", manifest=HYPERI) == "2.3"
        assert resolve_program_horizon("dfe", "next-major", manifest=HYPERI) == "3.0"

    def test_released_is_the_last_ga_not_the_train(self, dfe_infra: dict) -> None:
        assert program_released("dfe", manifest=HYPERI) == "2.2.0"
        dfe_infra["versions.yaml"] = _VERSIONS.format(
            current="2.2.2-rc.1", latest="2.2.1"
        )
        assert program_released("dfe", manifest=HYPERI) == "2.2.1"
        assert program_version("dfe", manifest=HYPERI) == "2.2.2-rc.1"

    @pytest.mark.parametrize("latest", ["2.2", "latest"])
    def test_a_released_value_that_is_not_semver_raises(
        self, dfe_infra: dict, latest: str
    ) -> None:
        dfe_infra["versions.yaml"] = _VERSIONS.format(
            current="2.2.1-rc.17", latest=latest
        )
        with pytest.raises(ProgramError, match="`latest` is") as raised:
            program_released("dfe", manifest=HYPERI)
        assert not isinstance(raised.value, NoReleaseYetError)
        assert program_version("dfe", manifest=HYPERI) == "2.2.1-rc.17"

    @pytest.mark.parametrize("latest", ['""', "", "null"])
    def test_an_empty_released_value_is_no_release_yet(
        self, dfe_infra: dict, latest: str
    ) -> None:
        dfe_infra["versions.yaml"] = (
            f'schema: 2\ncurrent: "2.2.0-rc.3"\nlatest: {latest}\n'
        )
        with pytest.raises(NoReleaseYetError, match="no GA release yet"):
            program_released("dfe", manifest=HYPERI)

    def test_released_reports_an_unreadable_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(repo: str, path: str) -> str | None:
            raise RepoFileError(f"could not read {repo}:{path}: HTTP 502")

        monkeypatch.setattr(gh, "read_repo_file", fail)
        with pytest.raises(ProgramError, match="HTTP 502"):
            program_released("dfe", manifest=HYPERI)

    def test_a_program_has_no_backlog_horizon(self, dfe_infra: dict) -> None:
        with pytest.raises(ProgramError, match="no 'backlog' horizon"):
            resolve_program_horizon("dfe", "backlog", manifest=HYPERI)

    def test_a_missing_source_raises(self, dfe_infra: dict) -> None:
        dfe_infra["suite.yaml"] = None
        with pytest.raises(ProgramError, match="does not exist"):
            program_members("dfe", manifest=HYPERI)

    def test_an_unreadable_source_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(repo: str, path: str) -> str | None:
            raise RepoFileError(f"could not read {repo}:{path}: HTTP 502")

        monkeypatch.setattr(gh, "read_repo_file", fail)
        with pytest.raises(ProgramError, match="HTTP 502"):
            program_members("dfe", manifest=HYPERI)
        with pytest.raises(ProgramError, match="HTTP 502"):
            resolve_program_horizon("dfe", "next-patch", manifest=HYPERI)

    def test_no_gh_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(repo: str, path: str) -> str | None:
            raise FileNotFoundError("gh")

        monkeypatch.setattr(gh, "read_repo_file", missing)
        with pytest.raises(ProgramError, match="could not read"):
            program_version("dfe", manifest=HYPERI)

    @pytest.mark.parametrize(
        ("path", "text", "message"),
        [
            ("suite.yaml", "nodes: [", "not valid YAML"),
            ("suite.yaml", "schema: '1'\n", "has no `nodes`"),
            ("suite.yaml", "nodes: {}\n", "non-empty mapping"),
            ("suite.yaml", "nodes:\n  x:\n    role: infra\n", "owner/name"),
        ],
    )
    def test_a_malformed_membership_source_raises(
        self, dfe_infra: dict, path: str, text: str, message: str
    ) -> None:
        dfe_infra[path] = text
        with pytest.raises(ProgramError, match=message):
            program_members("dfe", manifest=HYPERI)

    @pytest.mark.parametrize("text", ["schema: 2\n", "current: 2.3\n", "current: x\n"])
    def test_a_malformed_version_source_raises(
        self, dfe_infra: dict, text: str
    ) -> None:
        dfe_infra["versions.yaml"] = text
        with pytest.raises(ProgramError):
            resolve_program_horizon("dfe", "next-minor", manifest=HYPERI)

    def test_check_accepts_program_labels_and_reports_their_drift(self) -> None:
        family = HYPERI.programs["dfe"].labels
        present = [
            *_all_expected(manifest=HYPERI),
            _label(family[0].name, family[0].color, family[0].description),
            _label(family[1].name, "ededed", family[1].description),
        ]
        report = Report()
        check_labels(present, {}, report, manifest=HYPERI)
        assert not report.extra
        assert not report.missing
        assert [(d.name, d.field) for d in report.drift] == [
            ("dfe:next-minor", "color")
        ]


class TestJson:
    """`labels --json` is read by consumers that cannot import hyperi_ci."""

    @pytest.fixture(autouse=True)
    def _dfe_infra(self, dfe_infra: dict) -> None:
        """Keep the program reads off the network."""

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
        "horizons": list,
        "programs": dict,
        "source": dict,
    }

    def test_the_top_level_keys_and_types_are_pinned(self) -> None:
        doc = manifest_json(manifest=HYPERI)
        assert set(doc) == set(self._SHAPE)
        for key, kind in self._SHAPE.items():
            assert type(doc[key]) is kind, key
        assert doc["schema"] == 1

    def test_source_names_where_the_manifest_came_from(self) -> None:
        assert manifest_json(manifest=HYPERI)["source"] == {
            "kind": "file",
            "org": "",
            "path": str(HYPERI_FILE),
            "sha": "",
        }
        assert manifest_json()["source"] == {
            "kind": "packaged-default",
            "org": "",
            "path": "hyperi_ci/config/labels.yaml",
            "sha": "",
        }

    def test_the_command_reads_a_named_manifest(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            app,
            ["labels", "--json", "-C", str(tmp_path), "--manifest", str(HYPERI_FILE)],
        )
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert doc["source"]["kind"] == "file"
        assert list(doc["programs"]) == ["dfe"]

    def test_an_unreadable_named_manifest_exits_two(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.yaml"
        result = CliRunner().invoke(
            app, ["labels", "-C", str(tmp_path), "--manifest", str(missing)]
        )
        assert result.exit_code == 2

    def test_nested_shapes(self) -> None:
        doc = manifest_json({"docs": ("docs/",)})
        assert [g["name"] for g in doc["gate"]] == list(gate_labels())
        assert set(doc["gate"][0]) == {"name", "color", "description"}
        assert set(doc["types"][0]) == {"name", "use_for", "not_for"}
        assert set(doc["allowed"]) == {"patterns", "literals"}
        assert doc["area"] == {"prefix": "area/", "color": "bfdadc"}
        assert doc["areas"] == {"docs": ["docs/"]}

    def test_horizons(self) -> None:
        horizons = manifest_json(manifest=HYPERI)["horizons"]
        assert [h["name"] for h in horizons] == list(gate_labels())
        assert [h["rank"] for h in horizons] == [1, 2, 3, 4, 5]
        assert set(horizons[0]) == {"name", "rank", "set_by", "description"}
        set_by = {h["name"]: h["set_by"] for h in horizons}
        assert set_by == {
            "release-blocker": "hand",
            "next-patch": "anyone",
            "next-minor": "release-owner",
            "next-major": "release-owner",
            "backlog": "anyone",
        }

    def test_next_major_is_release_owner_only(self) -> None:
        doc = manifest_json(manifest=HYPERI)
        major = next(h for h in doc["horizons"] if h["name"] == "next-major")
        assert major["set_by"] == "release-owner"
        assert "never proposed by agents" in major["description"]
        dfe_major = next(
            x for x in doc["programs"]["dfe"]["labels"] if x["horizon"] == "next-major"
        )
        assert "never proposed by agents" in dfe_major["description"]

    def test_a_program_carries_its_sources_and_resolved_lines(self) -> None:
        dfe = manifest_json(manifest=HYPERI)["programs"]["dfe"]
        assert set(dfe) == {
            "labels",
            "membership_source",
            "version_source",
            "version",
            "lines",
            "released_source",
            "released",
            "released_state",
            "error",
        }
        assert [x["name"] for x in dfe["labels"]] == [
            "dfe:next-patch",
            "dfe:next-minor",
            "dfe:next-major",
        ]
        assert set(dfe["labels"][0]) == {"name", "horizon", "color", "description"}
        assert dfe["membership_source"] == {
            "repo": "hyperi-io/dfe-infra",
            "path": "suite.yaml",
            "key": "nodes",
        }
        assert dfe["version_source"] == {
            "repo": "hyperi-io/dfe-infra",
            "path": "versions.yaml",
            "key": "current",
        }
        assert dfe["version"] == "2.2.1-rc.17"
        assert dfe["lines"] == {
            "next-patch": "2.2.1",
            "next-minor": "2.3",
            "next-major": "3.0",
        }
        assert dfe["released_source"] == {
            "repo": "hyperi-io/dfe-infra",
            "path": "versions.yaml",
            "key": "latest",
        }
        assert dfe["released"] == "2.2.0"
        assert dfe["released_state"] == "released"
        assert dfe["error"] == ""

    def test_a_bad_released_value_keeps_the_lines(self, dfe_infra: dict) -> None:
        dfe_infra["versions.yaml"] = _VERSIONS.format(
            current="2.2.1-rc.17", latest="2.2"
        )
        dfe = manifest_json(manifest=HYPERI)["programs"]["dfe"]
        assert dfe["lines"]["next-patch"] == "2.2.1"
        assert dfe["released"] == ""
        assert dfe["released_state"] == "error"
        assert "`latest` is '2.2'" in dfe["error"]

    def test_an_empty_released_value_is_a_state_not_an_error(
        self, dfe_infra: dict
    ) -> None:
        dfe_infra["versions.yaml"] = _VERSIONS.format(current="2.2.0-rc.3", latest="")
        doc = manifest_json(manifest=HYPERI)
        dfe = doc["programs"]["dfe"]
        assert doc["schema"] == 1
        assert dfe["released_state"] == "none"
        assert dfe["released"] == ""
        assert dfe["error"] == ""
        assert dfe["lines"]["next-minor"] == "2.2"

    def test_an_unresolvable_program_says_why(self, dfe_infra: dict) -> None:
        dfe_infra["versions.yaml"] = None
        dfe = manifest_json(manifest=HYPERI)["programs"]["dfe"]
        assert dfe["lines"] == {}
        assert dfe["version"] == ""
        assert dfe["released"] == ""
        assert dfe["released_state"] == "error"
        assert dfe["error"].count("versions.yaml does not exist") == 1
        assert len(dfe["labels"]) == 3

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


_SHA = "0123456789abcdef0123456789abcdef01234567"
_NOT_FOUND = "gh: Not Found (HTTP 404)"


class FakeDotGithub:
    """Answers the reads the org lookup makes of `<org>/.github`."""

    def __init__(self) -> None:
        self.path = "labels/labels.yaml"
        self.commits: ApiResult = ApiResult([{"sha": _SHA}], "", None)
        self.file: str | None | Exception = HYPERI_FILE.read_text(encoding="utf-8")
        self.calls: list[list[str]] = []
        self.reads: list[tuple[str, str, str | None]] = []

    def api(self, args: list[str], *, body: dict | None = None) -> ApiResult:
        self.calls.append(args)
        assert args == [f"repos/hyperi-io/.github/commits?path={self.path}&per_page=1"]
        return self.commits

    def read(self, repo: str, path: str, *, ref: str | None = None) -> str | None:
        self.reads.append((repo, path, ref))
        if isinstance(self.file, Exception):
            raise self.file
        return self.file


@pytest.fixture
def dotgithub(monkeypatch: pytest.MonkeyPatch) -> FakeDotGithub:
    """Turn the org lookup back on, with the shipped path, and serve its reads."""
    fake = FakeDotGithub()
    monkeypatch.delenv("HYPERCI_ISSUES_LABELS_PATH")
    load_config(reload=True, report_removed=False)
    monkeypatch.setattr(gh, "gh_api", fake.api)
    monkeypatch.setattr(gh, "read_repo_file", fake.read)
    return fake


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(common, "warn", said.append)
    return said


class TestOrgSource:
    """The manifest is read from the org's `.github` repo when it has one."""

    def test_the_org_file_wins_over_the_packaged_default(
        self, dotgithub: FakeDotGithub, warnings: list[str]
    ) -> None:
        manifest = load_manifest(org="hyperi-io")
        assert manifest == HYPERI
        assert manifest.source == ManifestSource(
            "org-dotgithub", "hyperi-io", "labels/labels.yaml", _SHA
        )
        assert dotgithub.reads == [("hyperi-io/.github", "labels/labels.yaml", _SHA)]
        assert warnings == []

    def test_a_named_file_wins_over_the_org(self, dotgithub: FakeDotGithub) -> None:
        manifest = load_manifest(HYPERI_FILE, org="hyperi-io")
        assert manifest.source.kind == "file"
        assert dotgithub.calls == []

    def test_the_path_comes_from_the_config_cascade(
        self, dotgithub: FakeDotGithub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_ISSUES_LABELS_PATH", "issue-labels.yaml")
        load_config(reload=True, report_removed=False)
        dotgithub.path = "issue-labels.yaml"
        assert load_manifest(org="hyperi-io").source.path == "issue-labels.yaml"

    def test_an_empty_path_turns_the_lookup_off(
        self, dotgithub: FakeDotGithub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_ISSUES_LABELS_PATH", "")
        load_config(reload=True, report_removed=False)
        assert load_manifest(org="hyperi-io").source.kind == "packaged-default"
        assert dotgithub.calls == []

    @pytest.mark.parametrize(
        ("commits", "file"),
        [
            (ApiResult(None, f"gh api failed: {_NOT_FOUND}", _NOT_FOUND), "unread"),
            (ApiResult([], "", None), "unread"),
            (ApiResult([{"sha": _SHA}], "", None), None),
        ],
        ids=["no-dotgithub-repo", "no-file", "file-deleted"],
    )
    def test_absence_falls_to_the_default_silently(
        self,
        dotgithub: FakeDotGithub,
        warnings: list[str],
        commits: ApiResult,
        file: str | None,
    ) -> None:
        dotgithub.commits = commits
        dotgithub.file = file
        manifest = load_manifest(org="hyperi-io")
        assert manifest.source.kind == "packaged-default"
        assert warnings == []
        if file == "unread":
            assert dotgithub.reads == []

    @pytest.mark.parametrize(
        ("commits", "file", "reason"),
        [
            (ApiResult(None, "gh api failed: HTTP 502", "HTTP 502"), "", "HTTP 502"),
            (ApiResult({"sha": _SHA}, "", None), "", "not a JSON array"),
            (ApiResult([{"commit": {}}], "", None), "", "no sha"),
            (
                ApiResult([{"sha": _SHA}], "", None),
                RepoFileError("could not read hyperi-io/.github: HTTP 401"),
                "HTTP 401",
            ),
            (
                ApiResult([{"sha": _SHA}], "", None),
                FileNotFoundError("gh"),
                "gh",
            ),
            (
                ApiResult([{"sha": _SHA}], "", None),
                "gate: [",
                "while parsing a flow node",
            ),
            (ApiResult([{"sha": _SHA}], "", None), "gate: []\n", "`bot_owned`"),
            (ApiResult([{"sha": _SHA}], "", None), "schema: 2\n", "`schema` is 2"),
        ],
        ids=[
            "commits-502",
            "commits-not-a-list",
            "commit-without-sha",
            "file-401",
            "no-gh",
            "not-yaml",
            "invalid-manifest",
            "unknown-schema",
        ],
    )
    def test_any_other_failure_warns_once_and_falls_back(
        self,
        dotgithub: FakeDotGithub,
        warnings: list[str],
        commits: ApiResult,
        file: str | Exception,
        reason: str,
    ) -> None:
        dotgithub.commits = commits
        dotgithub.file = file
        assert load_manifest(org="hyperi-io").source.kind == "packaged-default"
        assert load_manifest(org="hyperi-io").source.kind == "packaged-default"
        [line] = warnings
        assert "hyperi-io/.github:labels/labels.yaml" in line
        assert reason in line
        assert "\n" not in line

    def test_the_org_file_is_read_once_per_process(
        self, dotgithub: FakeDotGithub
    ) -> None:
        for _ in range(3):
            gate_of(["backlog"], manifest=load_manifest(org="hyperi-io"))
        assert len(dotgithub.calls) == 1
        assert len(dotgithub.reads) == 1

    def test_the_json_source_carries_the_commit(self, dotgithub: FakeDotGithub) -> None:
        doc = manifest_json(manifest=load_manifest(org="hyperi-io"))
        assert doc["source"] == {
            "kind": "org-dotgithub",
            "org": "hyperi-io",
            "path": "labels/labels.yaml",
            "sha": _SHA,
        }

    def test_check_names_the_source_and_its_commit(
        self, dotgithub: FakeDotGithub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(common, "info", said.append)
        answers = [
            ApiResult(_all_expected(manifest=HYPERI), "", None),
            ApiResult([_issue(1, ["next-patch"])], "", None),
        ]
        with (
            patch("hyperi_ci.gh.shutil.which", return_value="/usr/bin/gh"),
            patch.object(labels_check, "gh_api", side_effect=answers),
            patch.object(labels_check, "read_repo_file", return_value=None),
        ):
            result = CliRunner().invoke(
                app, ["labels", "--check", "--repo", "hyperi-io/x"]
            )
        assert result.exit_code == 0, result.output
        source = f"labels: manifest from hyperi-io/.github:labels/labels.yaml at {_SHA}"
        assert source in said


_BAD_502 = ApiResult(None, "gh api failed: HTTP 502", "HTTP 502")
_ONE_COMMIT = ApiResult([{"sha": _SHA}], "", None)


class TestStrictOrgSource:
    """``strict`` refuses an org file that exists but is unusable, and keeps a 404 silent."""

    @pytest.mark.parametrize(
        ("commits", "file", "reason"),
        [
            (_BAD_502, "", "HTTP 502"),
            (
                _ONE_COMMIT,
                RepoFileError("could not read hyperi-io/.github: HTTP 401"),
                "HTTP 401",
            ),
            (_ONE_COMMIT, "gate: [", "while parsing a flow node"),
            (_ONE_COMMIT, "gate: []\n", "`bot_owned`"),
            (_ONE_COMMIT, "schema: 2\n", "`schema` is 2"),
        ],
        ids=[
            "commits-502",
            "file-401",
            "not-yaml",
            "invalid-manifest",
            "unknown-schema",
        ],
    )
    def test_any_non_404_failure_raises_naming_the_source(
        self,
        dotgithub: FakeDotGithub,
        warnings: list[str],
        commits: ApiResult,
        file: str | Exception,
        reason: str,
    ) -> None:
        dotgithub.commits = commits
        dotgithub.file = file
        with pytest.raises(ManifestError) as caught:
            load_manifest(org="hyperi-io", strict=True)
        assert "hyperi-io/.github:labels/labels.yaml" in str(caught.value)
        assert reason in str(caught.value)
        assert warnings == []

    def test_a_404_still_falls_to_the_default_silently(
        self, dotgithub: FakeDotGithub, warnings: list[str]
    ) -> None:
        dotgithub.commits = ApiResult(None, f"gh api failed: {_NOT_FOUND}", _NOT_FOUND)
        manifest = load_manifest(org="hyperi-io", strict=True)
        assert manifest.source.kind == "packaged-default"
        assert warnings == []

    def test_a_cached_fallback_does_not_satisfy_a_strict_call(
        self, dotgithub: FakeDotGithub, warnings: list[str]
    ) -> None:
        dotgithub.commits = _BAD_502
        assert load_manifest(org="hyperi-io").source.kind == "packaged-default"
        with pytest.raises(ManifestError):
            load_manifest(org="hyperi-io", strict=True)
        assert len(dotgithub.calls) == 1
        assert len(warnings) == 1

    def test_a_good_org_file_is_returned_under_strict(
        self, dotgithub: FakeDotGithub
    ) -> None:
        assert load_manifest(org="hyperi-io", strict=True) == HYPERI


class TestStrictCommand:
    """`labels --check` and `--stale` exit 2 on an unusable org file, the listing does not."""

    @staticmethod
    def _invoke(*args: str, answers: list[ApiResult] | None = None):
        with (
            patch("hyperi_ci.gh.shutil.which", return_value="/usr/bin/gh"),
            patch.object(labels_check, "gh_api", side_effect=answers or []),
            patch.object(labels_check, "read_repo_file", return_value=None),
        ):
            return CliRunner().invoke(app, ["labels", *args, "--repo", "hyperi-io/x"])

    @pytest.fixture
    def errors(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        said: list[str] = []
        monkeypatch.setattr(common, "error", said.append)
        return said

    def test_check_exits_two_naming_the_source_and_the_error(
        self, dotgithub: FakeDotGithub, errors: list[str]
    ) -> None:
        dotgithub.commits = _BAD_502
        result = self._invoke("--check")
        assert result.exit_code == 2, result.output
        [line] = errors
        assert "hyperi-io/.github:labels/labels.yaml" in line
        assert "HTTP 502" in line

    def test_stale_exits_two(self, dotgithub: FakeDotGithub, errors: list[str]) -> None:
        dotgithub.file = "schema: 2\n"
        result = self._invoke("--stale")
        assert result.exit_code == 2, result.output
        assert any("`schema` is 2" in line for line in errors)

    def test_check_after_a_404_checks_against_the_neutral_default(
        self, dotgithub: FakeDotGithub, warnings: list[str]
    ) -> None:
        dotgithub.commits = ApiResult(None, f"gh api failed: {_NOT_FOUND}", _NOT_FOUND)
        answers = [
            ApiResult(_all_expected(), "", None),
            ApiResult([_issue(1, ["next-patch"])], "", None),
        ]
        result = self._invoke("--check", answers=answers)
        assert result.exit_code == 0, result.output
        assert warnings == []

    @pytest.mark.parametrize("mode", [(), ("--json",)], ids=["listing", "json"])
    def test_the_listing_and_json_still_warn_and_fall_back(
        self, dotgithub: FakeDotGithub, warnings: list[str], mode: tuple[str, ...]
    ) -> None:
        dotgithub.commits = _BAD_502
        result = self._invoke(*mode)
        assert result.exit_code == 0, result.output
        [line] = warnings
        assert "packaged default applies" in line


class TestResolveOrg:
    @pytest.fixture(autouse=True)
    def _outside_actions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("GITHUB_REPOSITORY_OWNER", "GITHUB_REPOSITORY"):
            monkeypatch.delenv(name, raising=False)

    def test_the_repo_owner_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "acme")
        assert resolve_org("other-org/x") == "other-org"

    def test_in_ci_the_repository_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "acme")
        assert resolve_org() == "acme"

    def test_locally_the_owner_of_the_origin_remote(self, tmp_path: Path) -> None:
        for args in (
            ["init", "-q"],
            ["remote", "add", "origin", "git@github.com:acme/widget.git"],
        ):
            run_cmd(["git", *args], capture=True, check=True, cwd=tmp_path)
        assert resolve_org(cwd=tmp_path) == "acme"

    def test_nothing_names_no_org(self, tmp_path: Path) -> None:
        assert resolve_org(cwd=tmp_path) == ""
