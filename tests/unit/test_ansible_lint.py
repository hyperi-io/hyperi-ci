# Project:   HyperI CI
# File:      tests/unit/test_ansible_lint.py
# Purpose:   Tests for the ansible dimension of lint-iac
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.ansible_lint.

Project selection, roles_path and the parsers run against real files, and the
command lines with run_cmd recorded. The real ansible-galaxy and ansible-lint
run is in test_lint_iac.py, ``TestTreeUnchanged``.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import ansible_lint, findings
from hyperi_ci.quality.targets import discover_ansible_projects


def _project(path: Path, cfg: str | None = "[defaults]\nroles_path = roles\n") -> Path:
    (path / "roles").mkdir(parents=True, exist_ok=True)
    (path / "playbooks").mkdir(exist_ok=True)
    if cfg is not None:
        (path / "ansible.cfg").write_text(cfg, encoding="utf-8")
    return path


class TestProjects:
    def test_cfg_or_playbooks_plus_roles_marks_a_project(self, tmp_path: Path) -> None:
        a = _project(tmp_path / "ansible")
        b = _project(tmp_path / "other", cfg=None)
        (tmp_path / "just-roles" / "roles").mkdir(parents=True)
        assert discover_ansible_projects(tmp_path) == [a, b]

    def test_exclude_paths_in_the_repo_config_drop_a_project(
        self, tmp_path: Path
    ) -> None:
        keep = _project(tmp_path / "ansible")
        drop = _project(tmp_path / "deprecated" / "old")
        (tmp_path / ".ansible-lint").write_text(
            "exclude_paths:\n  - deprecated/\n", encoding="utf-8"
        )
        assert ansible_lint.lintable_projects([keep, drop], tmp_path) == [keep]


class TestRolesPath:
    def test_reads_ansible_cfg_relative_to_the_project(self, tmp_path: Path) -> None:
        p = _project(tmp_path / "a", cfg="[defaults]\nroles_path = roles:../shared\n")
        assert ansible_lint.roles_path(p) == [
            (p / "roles").resolve(),
            (tmp_path / "shared").resolve(),
        ]

    def test_without_cfg_falls_back_to_roles_dir(self, tmp_path: Path) -> None:
        p = _project(tmp_path / "a", cfg=None)
        assert ansible_lint.roles_path(p) == [(p / "roles").resolve()]

    def test_requirement_files_found_in_the_known_places(self, tmp_path: Path) -> None:
        p = _project(tmp_path / "a")
        (p / "requirements.yml").write_text("collections: []\n", encoding="utf-8")
        (p / "collections").mkdir()
        (p / "collections" / "requirements.yml").write_text("[]\n", encoding="utf-8")
        (p / "roles" / "vendored").mkdir()
        (p / "roles" / "vendored" / "requirements.yml").write_text("", encoding="utf-8")
        assert ansible_lint.requirement_files(p) == [
            p / "requirements.yml",
            p / "collections" / "requirements.yml",
        ]


class TestYamllintParse:
    def test_parses_parsable_format(self) -> None:
        out = (
            "./a.yml:3:1: [error] wrong indentation (indentation)\n"
            "./b.yml:7:9: [warning] truthy value (truthy)\n"
            "noise\n"
        )
        found = ansible_lint.parse_yamllint(out)
        assert [(f.path, f.line, f.level, f.rule) for f in found] == [
            ("./a.yml", 3, "error", "indentation"),
            ("./b.yml", 7, "warning", "truthy"),
        ]


def _sarif(level: str) -> str:
    return json.dumps(
        {
            "runs": [
                {
                    "tool": {"driver": {"name": "ansible-lint"}},
                    "results": [
                        {
                            "ruleId": "fqcn[action-core]",
                            "level": level,
                            "message": {"text": "use FQCN"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "a/site.yml"},
                                        "region": {"startLine": 2},
                                    }
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Record every call; ansible-lint writes the SARIF ``state['level']`` asks for."""
    state: dict = {"calls": [], "level": "error"}

    def _run(cmd: list[str], **kw: object) -> SimpleNamespace:
        state["calls"].append((cmd, kw))
        if "--sarif-file" in cmd:
            target = Path(cmd[cmd.index("--sarif-file") + 1])
            target.write_text(_sarif(state["level"]), encoding="utf-8")
            return SimpleNamespace(returncode=2, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(findings, "run_cmd", _run)
    monkeypatch.setattr(ansible_lint, "_available", lambda cmd: True)
    return state


class TestRun:
    def test_lint_is_offline_with_roles_path_and_a_memory_cap(
        self, recorded: dict, tmp_path: Path
    ) -> None:
        p = _project(tmp_path / "ansible")
        ansible_lint.run(
            [p],
            CIConfig(_raw={}),
            root=tmp_path,
            scratch=tmp_path,
            timeout=9,
            memory_limit_bytes=1024,
        )
        cmd, kw = next(c for c in recorded["calls"] if "--sarif-file" in c[0])
        assert "--offline" in cmd
        assert cmd[-1] == "ansible"
        scratch_roles = str(tmp_path / "ansible" / "roles")
        assert kw["env"]["ANSIBLE_ROLES_PATH"].split(":") == [
            scratch_roles,
            str((p / "roles").resolve()),
        ]
        assert kw["env"]["ANSIBLE_COLLECTIONS_PATH"] == str(
            tmp_path / "ansible" / "collections"
        )
        assert kw["memory_limit_bytes"] == 1024
        assert kw["timeout"] == 9

    def test_galaxy_installs_into_scratch_from_a_project_below_the_root(
        self, recorded: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        scratch = tmp_path / "scratch"
        p = _project(repo / "ansible")
        (p / "requirements.yml").write_text("collections: []\n", encoding="utf-8")
        monkeypatch.chdir(repo)
        ansible_lint.run(
            [Path("ansible")], CIConfig(_raw={}), root=Path("."), scratch=scratch
        )
        galaxy = [(c, kw) for c, kw in recorded["calls"] if "install" in c]
        assert len(galaxy) == 1
        cmd, kw = galaxy[0]
        assert cmd[-1] == str((repo / "ansible" / "requirements.yml").resolve())
        assert Path(cmd[-1]).is_file()
        assert kw["cwd"] == scratch / "ansible"
        assert kw["env"]["ANSIBLE_COLLECTIONS_PATH"] == str(
            scratch / "ansible" / "collections"
        )
        assert kw["env"]["ANSIBLE_ROLES_PATH"] == str(scratch / "ansible" / "roles")
        assert not (scratch / "ansible").exists()

    def test_warn_by_default_blocking_when_promoted(
        self, recorded: dict, tmp_path: Path
    ) -> None:
        p = _project(tmp_path / "ansible")
        assert (
            ansible_lint.run([p], CIConfig(_raw={}), root=tmp_path, scratch=tmp_path)
            == 0
        )
        blocking = CIConfig(_raw={"quality": {"ansible_lint": "blocking"}})
        assert ansible_lint.run([p], blocking, root=tmp_path, scratch=tmp_path) == 1

    def test_warning_level_findings_never_fail(
        self, recorded: dict, tmp_path: Path
    ) -> None:
        recorded["level"] = "warning"
        p = _project(tmp_path / "ansible")
        blocking = CIConfig(_raw={"quality": {"ansible_lint": "blocking"}})
        assert ansible_lint.run([p], blocking, root=tmp_path, scratch=tmp_path) == 0

    def test_yamllint_runs_only_with_a_repo_config(
        self, recorded: dict, tmp_path: Path
    ) -> None:
        p = _project(tmp_path / "ansible")
        ansible_lint.run([p], CIConfig(_raw={}), root=tmp_path, scratch=tmp_path)
        assert not any("parsable" in c for c, _ in recorded["calls"])
        (tmp_path / ".yamllint").write_text("extends: default\n", encoding="utf-8")
        ansible_lint.run([p], CIConfig(_raw={}), root=tmp_path, scratch=tmp_path)
        assert any("parsable" in c for c, _ in recorded["calls"])

    def test_missing_tool_fails_in_ci_only_when_blocking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ansible_lint, "_available", lambda cmd: False)
        monkeypatch.setenv("CI", "true")
        p = _project(tmp_path / "ansible")
        warn_rc = ansible_lint.run(
            [p], CIConfig(_raw={}), root=tmp_path, scratch=tmp_path
        )
        assert warn_rc == 0
        blocking = CIConfig(_raw={"quality": {"ansible_lint": "blocking"}})
        assert ansible_lint.run([p], blocking, root=tmp_path, scratch=tmp_path) == 1
