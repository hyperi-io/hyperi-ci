# Project:   HyperI CI
# File:      tests/unit/test_release_prepare.py
# Purpose:   Repo code runs in release-prepare; the upload only uploads
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The prepare/upload split behind issue #409.

The upload holds every publish credential, so these pin two things: the
checks and packaging that execute repo code happen in ``release-prepare``, and
``run release`` with a prepared directory runs none of them. The prepared
directory comes from a job that ran repo code, so it is also checked as
untrusted input.
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from hyperi_ci import config as config_module
from hyperi_ci import dispatch, release_prepare
from hyperi_ci.common import run_cmd
from hyperi_ci.release_prepare import (
    MANIFEST_NAME,
    PREPARED_ENV,
    Phase,
    Prepared,
    PreparedError,
    prepare_release,
    restore_stamped,
)

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]

# Writes the version into a generated file, the shape of dfe-engine's spec.
_WRITE_SPEC = (
    "from pathlib import Path; "
    "Path('spec.json').write_text(Path('VERSION').read_text().strip())"
)


@pytest.fixture(autouse=True)
def _fresh_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_config_cache", None)
    monkeypatch.delenv(PREPARED_ENV, raising=False)


def _python_project(root: Path, *, track_version: bool, release: dict) -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    (root / ".hyperi-ci.yaml").write_text(
        yaml.safe_dump({"release": release}), encoding="utf-8"
    )
    run_cmd([*_GIT, "init", "-q"], cwd=root)
    if track_version:
        (root / "VERSION").write_text("0.0.0\n", encoding="utf-8")
    run_cmd([*_GIT, "add", "-A"], cwd=root)
    run_cmd([*_GIT, "commit", "-q", "-m", "init"], cwd=root)


def _write_prepared(root: Path, **overrides: Any) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 2, "version": "1.2.3", "language": "rust", "facts": {}}
    manifest.update(overrides)
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return root


class TestPrepareRelease:
    def test_it_stamps_and_carries_the_stamp_paths(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        project.mkdir()
        _python_project(
            project,
            track_version=True,
            release={
                "stamp_cmd": [sys.executable, "-c", _WRITE_SPEC],
                "stamp_paths": ["spec.json"],
            },
        )
        out = tmp_path / "prepared"

        assert prepare_release("v2.0.1", out_dir=out, project_dir=project) == 0

        manifest = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
        assert manifest["version"] == "2.0.1"
        assert manifest["language"] == "python"
        assert manifest["head"] == release_prepare.head_commit(project)
        assert len(manifest["head"]) == 40
        assert (out / "stamped" / "spec.json").read_text(encoding="utf-8") == "2.0.1"

    def test_version_is_never_carried(self, tmp_path: Path) -> None:
        """release-commit writes VERSION from the release version itself."""
        project = tmp_path / "repo"
        project.mkdir()
        _python_project(
            project, track_version=True, release={"stamp_paths": ["VERSION"]}
        )
        out = tmp_path / "prepared"

        assert prepare_release("2.0.1", out_dir=out, project_dir=project) == 0
        assert not (out / "stamped" / "VERSION").exists()

    def test_the_phases_split_the_outputs(self, tmp_path: Path) -> None:
        """The stamp outputs can leave the job before packaging code runs."""
        project = tmp_path / "repo"
        project.mkdir()
        _python_project(
            project,
            track_version=True,
            release={
                "stamp_cmd": [sys.executable, "-c", _WRITE_SPEC],
                "stamp_paths": ["spec.json"],
            },
        )
        stamped = tmp_path / "stamped"
        packaged = tmp_path / "packaged"

        assert (
            prepare_release(
                "2.0.1", out_dir=stamped, phase=Phase.STAMP, project_dir=project
            )
            == 0
        )
        assert (stamped / "spec.json").is_file()
        assert not (stamped / MANIFEST_NAME).exists()
        # Always present, so the workflow can require the artefact.
        assert (stamped / release_prepare.STAMP_MARKER).is_file()

        assert (
            prepare_release(
                "2.0.1", out_dir=packaged, phase=Phase.PACKAGE, project_dir=project
            )
            == 0
        )
        assert (packaged / MANIFEST_NAME).is_file()
        assert not (packaged / "stamped").exists()

    def test_a_failing_stamp_command_fails_prepare(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        project.mkdir()
        _python_project(
            project,
            track_version=True,
            release={"stamp_cmd": [sys.executable, "-c", "raise SystemExit(4)"]},
        )
        out = tmp_path / "prepared"

        assert prepare_release("2.0.1", out_dir=out, project_dir=project) == 1
        assert not (out / MANIFEST_NAME).exists()

    def test_an_empty_version_is_refused(self, tmp_path: Path) -> None:
        assert prepare_release("", out_dir=tmp_path / "o", project_dir=tmp_path) == 1


class TestLoad:
    def test_unset_is_none(self) -> None:
        assert release_prepare.load() is None

    def test_a_missing_manifest_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREPARED_ENV, str(tmp_path))
        with pytest.raises(PreparedError):
            release_prepare.load()

    def test_another_schema_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREPARED_ENV, str(_write_prepared(tmp_path, schema=99)))
        with pytest.raises(PreparedError):
            release_prepare.load()

    def test_a_path_out_of_the_directory_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "secret").write_text("x", encoding="utf-8")
        prepared = Prepared(root=tmp_path / "p", version="1", language="rust")
        (tmp_path / "p").mkdir()
        with pytest.raises(PreparedError):
            prepared.file("../secret")

    def test_a_symlink_is_refused_even_inside_the_directory(
        self, tmp_path: Path
    ) -> None:
        """The target resolves inside, so only the is_symlink check stops it."""
        (tmp_path / "p").mkdir()
        (tmp_path / "p" / "real.tgz").write_text("x", encoding="utf-8")
        (tmp_path / "p" / "link.tgz").symlink_to(tmp_path / "p" / "real.tgz")
        prepared = Prepared(root=tmp_path / "p", version="1", language="rust")
        assert prepared.file("real.tgz").is_file()
        with pytest.raises(PreparedError, match="symlink"):
            prepared.file("link.tgz")


class TestRestoreStamped:
    def test_only_the_named_files_leave_the_directory(self, tmp_path: Path) -> None:
        """The prepare job ran repo code, so it may have planted anything."""
        prepared_root = tmp_path / "prepared"
        stamped = prepared_root / "stamped"
        (stamped / ".git").mkdir(parents=True)
        (stamped / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        (stamped / "spec.json").write_text("{}", encoding="utf-8")
        (stamped / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (stamped / "Makefile").write_text("pwned:\n", encoding="utf-8")
        checkout = tmp_path / "checkout"
        (checkout / ".git").mkdir(parents=True)
        prepared = Prepared(root=prepared_root, version="9.9.9", language="python")

        restored = restore_stamped(
            prepared,
            checkout,
            ["VERSION", "spec.json", ".git/config", "missing.json"],
        )

        assert restored == ["spec.json"]
        assert not (checkout / "VERSION").exists()
        assert not (checkout / ".git" / "config").exists()
        assert not (checkout / "Makefile").exists()


class TestTheUploadChecksWhatItWasHanded:
    def test_a_version_mismatch_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREPARED_ENV, str(_write_prepared(tmp_path)))
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.4")
        assert dispatch.check_prepared("rust") == 1

    def test_a_language_mismatch_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREPARED_ENV, str(_write_prepared(tmp_path)))
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.3")
        assert dispatch.check_prepared("python") == 1

    def test_a_match_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREPARED_ENV, str(_write_prepared(tmp_path)))
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.3")
        assert dispatch.check_prepared("rust") == 0

    def test_ci_without_a_prepared_directory_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A caller on an old tail runs repo code beside the tokens."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("CI", "true")
        assert dispatch.check_prepared("rust") == 0
        assert "::warning title=hyperi-ci release without a prepare job::" in (
            capsys.readouterr().out
        )


class TestCommands:
    def test_release_verify_fails_a_mismatch_before_any_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from hyperi_ci.cli import app

        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "c"\n', encoding="utf-8"
        )
        monkeypatch.setenv(PREPARED_ENV, str(_write_prepared(tmp_path / "p")))
        monkeypatch.setenv("HYPERCI_VERSION", "9.9.9")
        result = CliRunner().invoke(app, ["release-verify", "-C", str(tmp_path)])
        assert result.exit_code == 1

    def test_an_unknown_phase_is_refused(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from hyperi_ci.cli import app

        result = CliRunner().invoke(
            app, ["release-prepare", "1.0.0", "--out", str(tmp_path), "--phase", "x"]
        )
        assert result.exit_code == 1
