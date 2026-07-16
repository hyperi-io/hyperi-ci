# Project:   HyperI CI
# File:      tests/unit/test_fixture_git.py
# Purpose:   Tests for the ci-test-* fixture git wrapper scope + refusals
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/fixture-git.py - the scope check and refusal policy.

The wrapper is allow-listed so fixture git runs unattended. Its whole value
is that it CANNOT be turned against a real repo - it only touches ci-test-*
paths and refuses scope-escape flags, while allowing any git op on a fixture.
Those pure predicates are locked here.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fixture_git",
    Path(__file__).resolve().parents[2] / "scripts" / "fixture-git.py",
)
assert _SPEC is not None and _SPEC.loader is not None  # always resolves for a real file
fixture_git = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixture_git)


def _make_repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


class TestIsFixture:
    def test_ci_test_repo_with_git_is_a_fixture(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ci-test-go-simple")
        assert fixture_git.is_fixture(repo) is True

    def test_non_ci_test_name_is_not_a_fixture(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "hyperi-ci")
        assert fixture_git.is_fixture(repo) is False

    def test_ci_test_name_without_git_is_not_a_fixture(self, tmp_path: Path) -> None:
        repo = tmp_path / "ci-test-go-simple"
        repo.mkdir()
        assert fixture_git.is_fixture(repo) is False


class TestForbiddenReason:
    def test_empty_args_refused(self) -> None:
        assert fixture_git.forbidden_reason([]) is not None

    @pytest.mark.parametrize(
        "args",
        [
            ["status"],
            ["status", "--short"],
            ["add", "."],
            ["commit", "-m", "fix: x"],
            ["push", "origin", "main"],
            # any git op ON the fixture is allowed - it is a throwaway repo.
            ["push", "-f", "origin", "main"],
            ["push", "--force-with-lease", "origin", "main"],
            ["push", "origin", "+refs/heads/main:refs/heads/main"],
            ["reset", "--hard", "HEAD~1"],
            ["clean", "-fdx"],
            ["rebase", "-i", "HEAD~3"],
            ["filter-repo", "--path", "x"],
        ],
    )
    def test_any_git_op_on_fixture_allowed(self, args: list[str]) -> None:
        assert fixture_git.forbidden_reason(args) is None

    @pytest.mark.parametrize(
        "args",
        [
            ["-C", "/etc", "status"],
            ["--git-dir=/somewhere/.git", "log"],
            ["status", "--work-tree", "/"],
        ],
    )
    def test_scope_escape_refused(self, args: list[str]) -> None:
        # not a restriction on fixture git - it would point git at another repo.
        assert fixture_git.forbidden_reason(args) is not None


class TestResolveFixture:
    def test_env_root_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_repo(tmp_path, "ci-test-rust-lib")
        monkeypatch.setenv("HYPERCI_FIXTURES_DIR", str(tmp_path))
        assert (
            fixture_git.resolve_fixture("ci-test-rust-lib")
            == (tmp_path / "ci-test-rust-lib").resolve()
        )

    def test_existing_path_used_verbatim(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ci-test-ts-simple")
        assert fixture_git.resolve_fixture(str(repo)) == repo.resolve()


class TestListFixtures:
    def test_lists_only_fixtures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_repo(tmp_path, "ci-test-go-simple")
        _make_repo(tmp_path, "ci-test-rust-lib")
        _make_repo(tmp_path, "hyperi-ci")  # not a fixture
        (tmp_path / "ci-test-no-git").mkdir()  # fixture name, no .git
        monkeypatch.setenv("HYPERCI_FIXTURES_DIR", str(tmp_path))
        names = [p.name for p in fixture_git.list_fixtures()]
        assert names == ["ci-test-go-simple", "ci-test-rust-lib"]


class TestMainRefusesNonFixture:
    def test_main_refuses_a_real_repo(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _make_repo(tmp_path, "hyperi-ci")
        monkeypatch.setenv("HYPERCI_FIXTURES_DIR", str(tmp_path))
        # would delegate to git if it passed scope; scope must stop it first.
        called = False

        def _fail(*_a: object, **_k: object) -> None:
            nonlocal called
            called = True
            raise AssertionError("git must not run for a non-fixture")

        monkeypatch.setattr(subprocess, "run", _fail)
        rc = fixture_git.main([str(tmp_path / "hyperi-ci"), "status"])
        assert rc == 3
        assert called is False
        assert "not a ci-test-*" in capsys.readouterr().err
