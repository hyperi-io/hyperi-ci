# Project:   HyperI CI
# File:      tests/unit/test_stamp.py
# Purpose:   Tests for central version stamping (VERSION + manifest)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Central `stamp_version`: writes VERSION (language-agnostic), then
delegates the manifest stamp to the detected language."""

from __future__ import annotations

from hyperi_ci.stamp import stamp_version


class TestVersionFileWrite:
    """The VERSION write is the central, always-on part."""

    def test_writes_version_file(self, tmp_path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nversion = "0.0.0"\n')
        rc = stamp_version("1.2.3", project_dir=tmp_path)
        assert rc == 0
        assert (tmp_path / "VERSION").read_text() == "1.2.3\n"

    def test_strips_leading_v(self, tmp_path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nversion = "0.0.0"\n')
        stamp_version("v2.0.0", project_dir=tmp_path)
        assert (tmp_path / "VERSION").read_text() == "2.0.0\n"

    def test_empty_version_is_error(self, tmp_path) -> None:
        rc = stamp_version("", project_dir=tmp_path)
        assert rc == 1
        assert not (tmp_path / "VERSION").exists()

    def test_unknown_language_still_writes_version(self, tmp_path) -> None:
        # No manifest of any kind → language undetected, VERSION still written.
        rc = stamp_version("3.1.4", project_dir=tmp_path)
        assert rc == 0
        assert (tmp_path / "VERSION").read_text() == "3.1.4\n"


class TestRustManifestStamp:
    def test_stamps_package_version(self, tmp_path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\nversion = "0.0.0"\n\n[dependencies]\nfoo = "1.2.3"\n'
        )
        stamp_version("1.2.3", project_dir=tmp_path)
        txt = (tmp_path / "Cargo.toml").read_text()
        assert 'version = "1.2.3"' in txt
        # dependency pin must be untouched
        assert 'foo = "1.2.3"' in txt

    def test_stamps_workspace_package_version(self, tmp_path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace.package]\nversion = "0.0.0"\nedition = "2024"\n'
        )
        stamp_version("4.5.6", project_dir=tmp_path)
        assert 'version = "4.5.6"' in (tmp_path / "Cargo.toml").read_text()


class TestPythonManifestStamp:
    def test_stamps_static_project_version(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.0.0"\n\n'
            '[tool.poetry]\nversion = "0.0.0"\n'
        )
        stamp_version("7.8.9", project_dir=tmp_path)
        txt = (tmp_path / "pyproject.toml").read_text()
        # [project] version updated
        assert '[project]\nname = "x"\nversion = "7.8.9"' in txt

    def test_dynamic_version_left_untouched(self, tmp_path) -> None:
        # hatch-vcs / dynamic projects have no [project] version line — never insert.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndynamic = ["version"]\n'
        )
        stamp_version("7.8.9", project_dir=tmp_path)
        txt = (tmp_path / "pyproject.toml").read_text()
        assert "version =" not in txt.split("[project]")[1]
        # VERSION file still carries the truth
        assert (tmp_path / "VERSION").read_text() == "7.8.9\n"


class TestTypescriptManifestStamp:
    def test_stamps_package_json_version(self, tmp_path) -> None:
        (tmp_path / "tsconfig.json").write_text("{}")
        (tmp_path / "package.json").write_text(
            '{\n  "name": "x",\n  "version": "0.0.0"\n}\n'
        )
        stamp_version("1.0.1", project_dir=tmp_path)
        import json

        data = json.loads((tmp_path / "package.json").read_text())
        assert data["version"] == "1.0.1"
        assert data["name"] == "x"


class TestGolangManifestStamp:
    def test_no_manifest_version_just_writes_version_file(self, tmp_path) -> None:
        # Go versions via ldflags from the VERSION file — no manifest field.
        (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.23\n")
        rc = stamp_version("2.2.2", project_dir=tmp_path)
        assert rc == 0
        assert (tmp_path / "VERSION").read_text() == "2.2.2\n"
        # go.mod untouched
        assert "2.2.2" not in (tmp_path / "go.mod").read_text()
