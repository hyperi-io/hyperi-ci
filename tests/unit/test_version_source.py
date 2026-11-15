# Project:   HyperI CI
# File:      tests/unit/test_version_source.py
# Purpose:   The starting version comes from the manifest, never from VERSION
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""A tag-less repo's first version is derived, not read off a stale file.

Issue #85: `VERSION` is an artefact this tool writes, and the committed copy
froze in May 2026 across 14 repos. Anything that treated it as an input shipped
a version dozens of releases old. The replacement reads what the project
declares about itself.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci.version_source import (
    DEFAULT_SEED_VERSION,
    build_version,
    declared_version,
    latest_tag_version,
    seed_version,
    tag_version,
)
from tests.git_helpers import git, init_repo


class TestDeclaredVersion:
    """The manifests, in the shapes a project declares them."""

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            pytest.param(
                {"pyproject.toml": '[project]\nname = "thing"\nversion = "2.4.1"\n'},
                ("2.4.1", "pyproject.toml"),
                id="pep-621",
            ),
            pytest.param(
                {
                    "pyproject.toml": '[tool.poetry]\nname = "thing"\nversion = "0.3.0"\n'
                },
                ("0.3.0", "pyproject.toml"),
                id="poetry",
            ),
            pytest.param(
                # hyperi-ci's own pyproject: `dynamic = ["version"]` with hatch reading
                # VERSION. Reading the key anyway brings the stale-file bug back.
                {
                    "pyproject.toml": '[project]\nname = "thing"\n'
                    'dynamic = ["version"]\nversion = "9.9.9"\n'
                },
                None,
                id="a-dynamic-version-is-skipped",
            ),
            pytest.param(
                {"pyproject.toml": "[project\nname ="},
                None,
                id="unparseable-toml",
            ),
            pytest.param(
                {"Cargo.toml": '[package]\nname = "thing"\nversion = "1.15.10"\n'},
                ("1.15.10", "Cargo.toml"),
                id="cargo-package-version",
            ),
            pytest.param(
                {
                    "Cargo.toml": '[workspace.package]\nversion = "3.1.4"\n\n'
                    '[package]\nname = "thing"\nversion.workspace = true\n'
                },
                ("3.1.4", "Cargo.toml"),
                id="cargo-version-workspace-true",
            ),
            pytest.param(
                # A workspace root with no [package]: the dfe-archiver shape.
                {
                    "Cargo.toml": '[workspace]\nmembers = ["a", "b"]\n\n'
                    '[workspace.package]\nversion = "5.0.2"\n'
                },
                ("5.0.2", "Cargo.toml"),
                id="cargo-virtual-manifest",
            ),
            pytest.param(
                {"package.json": json.dumps({"name": "thing", "version": "0.9.12"})},
                ("0.9.12", "package.json"),
                id="package-json",
            ),
            pytest.param({"package.json": "{not json"}, None, id="unparseable-json"),
            pytest.param(
                {"pyproject.toml": '[project]\nname = "thing"\nversion = "0.1.0a1"\n'},
                None,
                id="rejects-a-pep-440-prerelease",
            ),
            # A release candidate's commit-back leaves a semver prerelease in the
            # manifest, so it seeds its core.
            pytest.param(
                {"Cargo.toml": '[package]\nname = "thing"\nversion = "0.4.0-rc.1"\n'},
                ("0.4.0", "Cargo.toml"),
                id="semver-prerelease-rc",
            ),
            pytest.param(
                {
                    "Cargo.toml": '[package]\nname = "thing"\nversion = "v0.4.0-beta.2"\n'
                },
                ("0.4.0", "Cargo.toml"),
                id="semver-prerelease-beta-with-v",
            ),
            pytest.param(
                {"package.json": json.dumps({"version": "1.0"})},
                None,
                id="rejects-a-two-part-version",
            ),
            pytest.param(
                {"package.json": json.dumps({"version": "v1.2.3"})},
                ("1.2.3", "package.json"),
                id="tolerates-a-leading-v",
            ),
            pytest.param(
                {
                    "pyproject.toml": '[project]\nname = "thing"\nversion = "1.0.0rc1"\n',
                    "Cargo.toml": '[package]\nname = "thing"\nversion = "1.0.0"\n',
                },
                ("1.0.0", "Cargo.toml"),
                id="falls-through-a-rejected-manifest-to-the-next",
            ),
        ],
    )
    def test_the_declared_version(
        self, tmp_path: Path, files: dict[str, str], expected: tuple[str, str] | None
    ) -> None:
        for name, text in files.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        assert declared_version(tmp_path) == expected


class TestSeedVersion:
    """seed_version always answers -- that is the point of a default."""

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            pytest.param({}, (DEFAULT_SEED_VERSION, "default"), id="greenfield"),
            pytest.param(
                # Go has no manifest version at all.
                {"go.mod": "module example.com/thing\n"},
                (DEFAULT_SEED_VERSION, "default"),
                id="go-project",
            ),
            pytest.param(
                {"Cargo.toml": '[package]\nname = "thing"\nversion = "2.0.0"\n'},
                ("2.0.0", "Cargo.toml"),
                id="a-declared-version-wins-over-the-default",
            ),
            pytest.param(
                # VERSION is an output of a release, never an input.
                {"VERSION": "2.3.10\n"},
                (DEFAULT_SEED_VERSION, "default"),
                id="ignores-the-version-file",
            ),
            pytest.param(
                {
                    "VERSION": "2.3.10\n",
                    "pyproject.toml": '[project]\nname = "thing"\nversion = "2.29.12"\n',
                },
                ("2.29.12", "pyproject.toml"),
                id="a-stale-version-file-cannot-override-the-manifest",
            ),
        ],
    )
    def test_the_seed(
        self, tmp_path: Path, files: dict[str, str], expected: tuple[str, str]
    ) -> None:
        for name, text in files.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        assert seed_version(tmp_path) == expected


class TestLatestTagVersion:
    """A wrong answer here is the version hatchling stamps into the wheel."""

    def test_a_release_wins_over_a_higher_prerelease(self, tmp_path: Path) -> None:
        init_repo(tmp_path, seed=True)
        git(tmp_path, "tag", "v1.1.1")
        git(tmp_path, "tag", "v1.1.2-beta.1")
        assert latest_tag_version(tmp_path) == "1.1.1"

    def test_a_prerelease_only_repo_resolves_to_none(self, tmp_path: Path) -> None:
        init_repo(tmp_path, seed=True)
        git(tmp_path, "tag", "v1.1.2-beta.1")
        assert latest_tag_version(tmp_path) is None


class TestBuildVersion:
    """The build back-end's version source, in every place a build starts."""

    def test_the_run_version_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_VERSION", "5.6.7")
        (tmp_path / "VERSION").write_text("1.1.1\n", encoding="utf-8")
        assert build_version(tmp_path) == "5.6.7"

    def test_a_leading_v_is_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_VERSION", "v5.6.7")
        assert build_version(tmp_path) == "5.6.7"

    def test_falls_to_the_stamped_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In CI the stamp step wrote it moments earlier; an sdist carries it."""
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        (tmp_path / "VERSION").write_text("2.9.10\n", encoding="utf-8")
        assert build_version(tmp_path) == "2.9.10"

    def test_falls_to_the_latest_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A developer building a checkout that was never stamped."""
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        with patch("hyperi_ci.version_source.latest_tag_version", return_value="4.0.1"):
            assert build_version(tmp_path) == "4.0.1"

    def test_falls_to_the_seed_on_a_tag_less_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        with patch("hyperi_ci.version_source.latest_tag_version", return_value=None):
            assert build_version(tmp_path) == DEFAULT_SEED_VERSION

    def test_a_junk_version_file_does_not_win(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A build must not stop because someone left a note in VERSION."""
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        (tmp_path / "VERSION").write_text("see the git tag\n", encoding="utf-8")
        with patch("hyperi_ci.version_source.latest_tag_version", return_value="4.0.1"):
            assert build_version(tmp_path) == "4.0.1"

    def test_always_returns_plain_semver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PEP 440 tolerates more than the tag format does."""
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        with patch("hyperi_ci.version_source.latest_tag_version", return_value=None):
            assert re.fullmatch(r"\d+\.\d+\.\d+", build_version(tmp_path))


class TestStdlibOnly:
    """The composite action loads this file by path, with nothing installed.

    A `from hyperi_ci...` import here would work in the test suite and fail in
    the plan job, where no pip install has run.
    """

    def test_imports_nothing_from_the_package(self) -> None:
        import hyperi_ci.version_source as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        imports = [
            line.strip()
            for line in source.splitlines()
            if line.startswith(("import ", "from "))
        ]
        assert imports
        assert not [line for line in imports if "hyperi_ci" in line]

    def test_loads_standalone_by_path(self) -> None:
        """Exactly how `.github/actions/predict-version/seed_version.py` does it."""
        import importlib.util

        import hyperi_ci.version_source as module

        spec = importlib.util.spec_from_file_location("standalone", module.__file__)
        assert spec is not None and spec.loader is not None
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        assert loaded.DEFAULT_SEED_VERSION == DEFAULT_SEED_VERSION


class TestTagVersion:
    """A re-published tag names its own version (issue #352)."""

    @pytest.mark.parametrize(
        ("tag", "version"),
        [
            ("v1.0.4", "1.0.4"),
            ("v10.20.30", "10.20.30"),
            ("v1.2.0-beta.1", "1.2.0-beta.1"),
            (" v1.0.4\n", "1.0.4"),
        ],
    )
    def test_strips_the_v(self, tag: str, version: str) -> None:
        assert tag_version(tag) == version

    @pytest.mark.parametrize(
        "tag",
        ["", "1.0.4", "v1.0", "v1.0.4.1", "refs/tags/v1.0.4", "v1.0.4-", "latest"],
    )
    def test_refuses_what_is_not_a_release_tag(self, tag: str) -> None:
        # Publish names the release `v{version}`, so only a tag that round-trips
        # to that name is re-publishable.
        assert tag_version(tag) is None
