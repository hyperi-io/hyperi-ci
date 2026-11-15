# Project:   HyperI CI
# File:      tests/unit/test_description_source.py
# Purpose:   One description, resolved from the manifest that owns the artefact
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""The description comes from the manifest, and a workspace has an answer too.

`org.opencontainers.image.description` shipped empty in every image because
nothing resolved one. The gap that made a workspace hard: `[package]` lives in
each member and members legitimately differ, so there is no repo-level string
unless `[workspace.package]` carries one.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.description_source import (
    github_description,
    manifest_description,
    repo_slug,
    resolve_description,
)


def _python(root: Path, body: str) -> Path:
    (root / "pyproject.toml").write_text(body, encoding="utf-8")
    return root


def _rust(root: Path, body: str) -> Path:
    (root / "Cargo.toml").write_text(body, encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "lib.rs").write_text("", encoding="utf-8")
    return root


class TestManifestReading:
    def test_pep_621(self, tmp_path: Path) -> None:
        _python(tmp_path, '[project]\nname = "t"\ndescription = "Does a thing"\n')
        assert manifest_description(tmp_path) == ("Does a thing", "pyproject.toml")

    def test_poetry(self, tmp_path: Path) -> None:
        _python(tmp_path, '[tool.poetry]\nname = "t"\ndescription = "Poetry thing"\n')
        assert manifest_description(tmp_path) == ("Poetry thing", "pyproject.toml")

    def test_rust_package(self, tmp_path: Path) -> None:
        _rust(tmp_path, '[package]\nname = "t"\ndescription = "A crate"\n')
        assert manifest_description(tmp_path) == ("A crate", "Cargo.toml")

    def test_node(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "t", "description": "A package"}), encoding="utf-8"
        )
        (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
        assert manifest_description(tmp_path) == ("A package", "package.json")

    def test_absent_field(self, tmp_path: Path) -> None:
        _python(tmp_path, '[project]\nname = "t"\n')
        assert manifest_description(tmp_path) is None

    def test_go_has_no_field(self, tmp_path: Path) -> None:
        """go.mod carries no description; pkg.go.dev renders the doc comment."""
        (tmp_path / "go.mod").write_text("module example.com/t\n", encoding="utf-8")
        assert manifest_description(tmp_path) is None

    def test_a_multiline_description_collapses(self, tmp_path: Path) -> None:
        """An OCI label is a single line."""
        _python(
            tmp_path,
            '[project]\nname = "t"\ndescription = """Does a thing\nover two lines"""\n',
        )
        found = manifest_description(tmp_path)
        assert found is not None
        assert found[0] == "Does a thing over two lines"


class TestCargoWorkspace:
    """A workspace has no [package] of its own -- [workspace.package] answers."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param(
                '[workspace]\nmembers = ["crates/a"]\n\n'
                '[workspace.package]\ndescription = "The repo-level text"\n',
                ("The repo-level text", "Cargo.toml"),
                id="virtual-manifest-uses-workspace-package",
            ),
            pytest.param(
                # Forced inheritance would flatten distinct crates on crates.io.
                '[workspace.package]\ndescription = "Repo level"\n\n'
                '[package]\nname = "t"\ndescription = "Member specific"\n',
                ("Member specific", "Cargo.toml"),
                id="a-member-keeps-its-own",
            ),
            pytest.param(
                # `description.workspace = true` parses as a table, not a string.
                '[workspace.package]\ndescription = "Shared text"\n\n'
                '[package]\nname = "t"\ndescription.workspace = true\n',
                ("Shared text", "Cargo.toml"),
                id="an-inheriting-member-resolves-to-the-workspace",
            ),
            pytest.param(
                # Reported, not silently blank.
                '[workspace]\nmembers = ["crates/a"]\n\n'
                '[workspace.package]\nversion = "1.0.0"\n',
                None,
                id="a-workspace-with-no-description-has-none",
            ),
        ],
    )
    def test_the_workspace_description(
        self, tmp_path: Path, body: str, expected: tuple[str, str] | None
    ) -> None:
        _rust(tmp_path, body)
        assert manifest_description(tmp_path) == expected


class TestResolutionOrder:
    def test_config_beats_the_manifest(self, tmp_path: Path) -> None:
        _python(tmp_path, '[project]\nname = "t"\ndescription = "From manifest"\n')
        config = CIConfig(_raw={"description": "From config"})
        assert resolve_description(config, root=tmp_path, allow_github=False) == (
            "From config",
            ".hyperi-ci.yaml",
        )

    def test_an_empty_config_value_does_not_win(self, tmp_path: Path) -> None:
        """The default is empty, so it must not shadow a real manifest field."""
        _python(tmp_path, '[project]\nname = "t"\ndescription = "From manifest"\n')
        config = CIConfig(_raw={"description": ""})
        assert resolve_description(config, root=tmp_path, allow_github=False) == (
            "From manifest",
            "pyproject.toml",
        )

    def test_falls_to_github(self, tmp_path: Path) -> None:
        with patch(
            "hyperi_ci.description_source.github_description",
            return_value="From GitHub",
        ):
            assert resolve_description(
                CIConfig(_raw={}), root=tmp_path, repo="o/r"
            ) == ("From GitHub", "GitHub")

    def test_unresolved_returns_none(self, tmp_path: Path) -> None:
        assert (
            resolve_description(CIConfig(_raw={}), root=tmp_path, allow_github=False)
            is None
        )

    def test_github_is_skippable(self, tmp_path: Path) -> None:
        """A local run must not shell out to gh just to read a label."""
        with patch("hyperi_ci.description_source.run_cmd") as spawned:
            resolve_description(CIConfig(_raw={}), root=tmp_path, allow_github=False)
        spawned.assert_not_called()


class TestRepoSlug:
    """GITHUB_REPOSITORY only exists in Actions, so local runs read the remote."""

    @pytest.fixture(autouse=True)
    def no_ci_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    def test_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/from-env")
        with patch("hyperi_ci.description_source.run_cmd") as spawned:
            assert repo_slug() == "o/from-env"
        spawned.assert_not_called()

    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:hyperi-io/hyperi-ci.git",
            "https://github.com/hyperi-io/hyperi-ci.git",
            "https://github.com/hyperi-io/hyperi-ci",
            "ssh://git@github.com/hyperi-io/hyperi-ci.git",
        ],
    )
    def test_parses_remote_forms(self, url: str) -> None:
        result = MagicMock(returncode=0, stdout=f"{url}\n")
        with patch("hyperi_ci.description_source.run_cmd", return_value=result):
            assert repo_slug() == "hyperi-io/hyperi-ci"

    def test_a_non_github_remote_is_none(self) -> None:
        """Codeberg is on the roadmap; do not guess a slug for it."""
        result = MagicMock(returncode=0, stdout="git@codeberg.org:o/r.git\n")
        with patch("hyperi_ci.description_source.run_cmd", return_value=result):
            assert repo_slug() is None

    def test_no_remote_is_none(self) -> None:
        result = MagicMock(returncode=128, stdout="")
        with patch("hyperi_ci.description_source.run_cmd", return_value=result):
            assert repo_slug() is None


class TestGithubDescription:
    def test_reads_the_repo_blurb(self) -> None:
        result = MagicMock(returncode=0, stdout="What the repo does\n")
        with patch("hyperi_ci.description_source.run_cmd", return_value=result):
            assert github_description("o/r") == "What the repo does"

    def test_an_empty_blurb_is_none(self) -> None:
        result = MagicMock(returncode=0, stdout="\n")
        with patch("hyperi_ci.description_source.run_cmd", return_value=result):
            assert github_description("o/r") is None

    def test_a_failed_lookup_is_none(self) -> None:
        result = MagicMock(returncode=1, stdout="")
        with patch("hyperi_ci.description_source.run_cmd", return_value=result):
            assert github_description("o/r") is None

    def test_no_repo_known(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        with patch("hyperi_ci.description_source.repo_slug", return_value=None):
            with patch("hyperi_ci.description_source.run_cmd") as spawned:
                assert github_description() is None
        spawned.assert_not_called()
