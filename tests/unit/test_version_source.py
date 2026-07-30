# Project:   HyperI CI
# File:      tests/unit/test_version_source.py
# Purpose:   The starting version comes from the manifest, never from VERSION
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""A tag-less repo's first version is derived, not read off a stale file.

Issue #85: `VERSION` is an artefact this tool writes, and the committed copy
froze in May 2026 across 14 repos. Anything that treated it as an input shipped
a version dozens of releases old. The replacement reads what the project
declares about itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci.version_source import (
    DEFAULT_SEED_VERSION,
    build_version,
    declared_version,
    seed_version,
)


class TestPythonManifest:
    """pyproject.toml, in both PEP 621 and Poetry shapes."""

    def test_reads_pep_621_version(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "thing"\nversion = "2.4.1"\n', encoding="utf-8"
        )
        assert declared_version(tmp_path) == ("2.4.1", "pyproject.toml")

    def test_reads_poetry_version(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "thing"\nversion = "0.3.0"\n', encoding="utf-8"
        )
        assert declared_version(tmp_path) == ("0.3.0", "pyproject.toml")

    def test_skips_a_dynamic_version(self, tmp_path: Path) -> None:
        """A dynamic version is computed by the back-end — there is none to read.

        hyperi-ci's own pyproject is this shape: `dynamic = ["version"]` with
        hatch reading the VERSION file. Reading the `version` key anyway would
        reintroduce the stale-file bug through the back door.
        """
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "thing"\ndynamic = ["version"]\nversion = "9.9.9"\n',
            encoding="utf-8",
        )
        assert declared_version(tmp_path) is None

    def test_survives_unparseable_toml(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project\nname =", encoding="utf-8")
        assert declared_version(tmp_path) is None


class TestRustManifest:
    """Cargo.toml, including the workspace-inheritance shapes."""

    def test_reads_package_version(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "thing"\nversion = "1.15.10"\n', encoding="utf-8"
        )
        assert declared_version(tmp_path) == ("1.15.10", "Cargo.toml")

    def test_follows_version_workspace_true(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace.package]\nversion = "3.1.4"\n\n'
            '[package]\nname = "thing"\nversion.workspace = true\n',
            encoding="utf-8",
        )
        assert declared_version(tmp_path) == ("3.1.4", "Cargo.toml")

    def test_reads_a_virtual_manifest(self, tmp_path: Path) -> None:
        """A workspace root with no [package] — the dfe-archiver shape."""
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["a", "b"]\n\n[workspace.package]\nversion = "5.0.2"\n',
            encoding="utf-8",
        )
        assert declared_version(tmp_path) == ("5.0.2", "Cargo.toml")


class TestNodeManifest:
    def test_reads_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "thing", "version": "0.9.12"}), encoding="utf-8"
        )
        assert declared_version(tmp_path) == ("0.9.12", "package.json")

    def test_survives_unparseable_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
        assert declared_version(tmp_path) is None


class TestRejectedVersions:
    """Only a plain X.Y.Z can seed a tag — the tag format is `v${version}`."""

    def test_rejects_a_pep_440_prerelease(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "thing"\nversion = "0.1.0a1"\n', encoding="utf-8"
        )
        assert declared_version(tmp_path) is None

    def test_rejects_a_two_part_version(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"version": "1.0"}), encoding="utf-8"
        )
        assert declared_version(tmp_path) is None

    def test_tolerates_a_leading_v(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"version": "v1.2.3"}), encoding="utf-8"
        )
        assert declared_version(tmp_path) == ("1.2.3", "package.json")

    def test_falls_through_a_rejected_manifest_to_the_next(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "thing"\nversion = "1.0.0rc1"\n', encoding="utf-8"
        )
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "thing"\nversion = "1.0.0"\n', encoding="utf-8"
        )
        assert declared_version(tmp_path) == ("1.0.0", "Cargo.toml")


class TestSeedVersion:
    """seed_version always answers — that is the point of a default."""

    def test_greenfield_starts_at_the_default(self, tmp_path: Path) -> None:
        assert seed_version(tmp_path) == (DEFAULT_SEED_VERSION, "default")

    def test_default_is_pre_one_point_oh(self) -> None:
        """A project that declares nothing has promised nothing."""
        assert DEFAULT_SEED_VERSION == "0.1.0"

    def test_go_project_gets_the_default(self, tmp_path: Path) -> None:
        """Go has no manifest version at all."""
        (tmp_path / "go.mod").write_text("module example.com/thing\n", encoding="utf-8")
        assert seed_version(tmp_path) == (DEFAULT_SEED_VERSION, "default")

    def test_declared_version_wins_over_the_default(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "thing"\nversion = "2.0.0"\n', encoding="utf-8"
        )
        assert seed_version(tmp_path) == ("2.0.0", "Cargo.toml")

    def test_ignores_the_version_file(self, tmp_path: Path) -> None:
        """The whole point of issue #85 — VERSION is an output, not an input."""
        (tmp_path / "VERSION").write_text("2.3.10\n", encoding="utf-8")
        assert seed_version(tmp_path) == (DEFAULT_SEED_VERSION, "default")

    def test_a_stale_version_file_cannot_override_the_manifest(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "VERSION").write_text("2.3.10\n", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "thing"\nversion = "2.29.12"\n', encoding="utf-8"
        )
        assert seed_version(tmp_path) == ("2.29.12", "pyproject.toml")


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
