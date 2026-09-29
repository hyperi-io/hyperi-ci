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
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from hyperi_ci import config as config_module
from hyperi_ci import dispatch, release_prepare
from hyperi_ci.common import run_cmd
from hyperi_ci.config import load_config
from hyperi_ci.languages.golang import release as go_release
from hyperi_ci.languages.python import release as py_release
from hyperi_ci.languages.rust import release as rust_release
from hyperi_ci.languages.typescript import release as ts_release
from hyperi_ci.release_prepare import (
    MANIFEST_NAME,
    PREPARED_ENV,
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
    manifest = {"schema": 1, "version": "1.2.3", "language": "rust", "facts": {}}
    manifest.update(overrides)
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return root


class TestPrepareRelease:
    def test_it_stamps_and_carries_version_and_stamp_paths(
        self, tmp_path: Path
    ) -> None:
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
        assert (out / "stamped" / "VERSION").read_text(encoding="utf-8") == "2.0.1\n"
        assert (out / "stamped" / "spec.json").read_text(encoding="utf-8") == "2.0.1"

    def test_an_untracked_version_file_is_not_carried(self, tmp_path: Path) -> None:
        """A repo that commits no VERSION has opted out of one."""
        project = tmp_path / "repo"
        project.mkdir()
        _python_project(project, track_version=False, release={})
        out = tmp_path / "prepared"

        assert prepare_release("2.0.1", out_dir=out, project_dir=project) == 0
        assert not (out / "stamped" / "VERSION").exists()

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

    def test_a_symlink_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "secret").write_text("x", encoding="utf-8")
        (tmp_path / "p").mkdir()
        (tmp_path / "p" / "link.tgz").symlink_to(tmp_path / "secret")
        prepared = Prepared(root=tmp_path / "p", version="1", language="rust")
        with pytest.raises(PreparedError):
            prepared.file("link.tgz")


class TestRestoreStamped:
    def test_only_the_named_files_leave_the_directory(self, tmp_path: Path) -> None:
        """The prepare job ran repo code, so it may have planted anything."""
        prepared_root = tmp_path / "prepared"
        stamped = prepared_root / "stamped"
        (stamped / ".git").mkdir(parents=True)
        (stamped / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        (stamped / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (stamped / "Makefile").write_text("pwned:\n", encoding="utf-8")
        checkout = tmp_path / "checkout"
        (checkout / ".git").mkdir(parents=True)
        prepared = Prepared(root=prepared_root, version="9.9.9", language="python")

        restored = restore_stamped(
            prepared, checkout, ["VERSION", ".git/config", "missing.json"]
        )

        assert restored == ["VERSION"]
        assert (checkout / "VERSION").read_text(encoding="utf-8") == "9.9.9\n"
        assert not (checkout / ".git" / "config").exists()
        assert not (checkout / "Makefile").exists()


class TestTheUploadChecksWhatItWasHanded:
    def test_a_version_mismatch_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREPARED_ENV, str(_write_prepared(tmp_path)))
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.4")
        assert dispatch._check_prepared("rust") == 1

    def test_a_language_mismatch_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREPARED_ENV, str(_write_prepared(tmp_path)))
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.3")
        assert dispatch._check_prepared("python") == 1

    def test_a_match_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREPARED_ENV, str(_write_prepared(tmp_path)))
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.3")
        assert dispatch._check_prepared("rust") == 0


class _Recorder:
    """Stands in for run_cmd and records each call's argv and working directory."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        cwd = kwargs.get("cwd")
        self.calls.append((cmd, Path(cwd) if cwd else None, kwargs.get("env")))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()


def _rust_config(root: Path) -> Any:
    (root / ".hyperi-ci.yaml").write_text(
        yaml.safe_dump({"release": {"enabled": True, "target": "oss"}}),
        encoding="utf-8",
    )
    return load_config(reload=True, project_dir=root)


class TestRustUpload:
    @pytest.fixture
    def crate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "crate"
        root.mkdir()
        (root / "Cargo.toml").write_text(
            '[package]\nname = "c"\nversion = "0.0.0"\n', encoding="utf-8"
        )
        monkeypatch.chdir(root)
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.3")
        monkeypatch.setenv("CARGO_REGISTRY_TOKEN", "fake-cargo-token")
        return root

    def test_prepared_upload_runs_no_check_and_no_cargo_query(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """semver-checks builds the crate; cargo metadata runs its toolchain file."""
        prepared = _write_prepared(tmp_path / "p", facts={"crate": True})
        monkeypatch.setenv(PREPARED_ENV, str(prepared))
        recorder = _Recorder()
        monkeypatch.setattr(rust_release, "run_cmd", recorder)

        def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the upload must not run this")

        monkeypatch.setattr(rust_release.semver_checks, "run", _forbidden)
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.build._detect_binary_names", _forbidden
        )

        assert rust_release.run(_rust_config(crate)) == 0

        ((cmd, cwd, _env),) = recorder.calls
        assert cmd[:2] == ["cargo", "publish"]
        assert "--no-verify" in cmd
        assert cmd[cmd.index("--manifest-path") + 1] == str(crate / "Cargo.toml")
        # Outside the crate, so neither its .cargo/config.toml nor its
        # rust-toolchain.toml is read.
        assert cwd is not None and not cwd.is_relative_to(crate)
        assert "fake-cargo-token" not in cmd
        assert 'version = "1.2.3"' in (crate / "Cargo.toml").read_text(encoding="utf-8")

    def test_a_prepared_no_crate_publishes_nothing(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prepared = _write_prepared(tmp_path / "p", facts={"crate": False})
        monkeypatch.setenv(PREPARED_ENV, str(prepared))
        recorder = _Recorder()
        monkeypatch.setattr(rust_release, "run_cmd", recorder)

        assert rust_release.run(_rust_config(crate)) == 0
        assert recorder.calls == []

    def test_prepare_runs_the_checks_and_packages(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Recorder()
        checked: list[bool] = []
        monkeypatch.setattr(rust_release, "run_cmd", recorder)
        monkeypatch.setattr(
            rust_release.semver_checks, "run", lambda _c: checked.append(True) or 0
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.build._detect_binary_names", lambda: []
        )

        rc, facts = rust_release.prepare(_rust_config(crate), tmp_path / "out")

        assert (rc, facts) == (0, {"crate": True})
        assert checked == [True]
        assert [c[0][:2] for c in recorder.calls] == [["cargo", "package"]]

    def test_prepare_says_a_binary_app_has_no_crate(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.build._detect_binary_names", lambda: ["app"]
        )
        rc, facts = rust_release.prepare(_rust_config(crate), tmp_path / "out")
        assert (rc, facts) == (0, {"crate": False})


class TestPythonUpload:
    def test_the_repo_config_and_argv_never_see_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`[tool.uv] publish-url` would otherwise send the token elsewhere."""
        monkeypatch.setenv("PYPI_TOKEN", "fake-pypi-token")
        recorder = _Recorder()
        monkeypatch.setattr(py_release, "run_cmd", recorder)

        assert py_release._publish_pypi() == 0

        ((cmd, _cwd, env),) = recorder.calls
        assert cmd == ["uv", "publish", "--no-config"]
        assert env == {"UV_PUBLISH_TOKEN": "fake-pypi-token"}


class TestGoUpload:
    def test_the_module_path_comes_from_go_mod(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text(
            "// header\nmodule github.com/hyperi-io/x // trailing\n\ngo 1.24\n",
            encoding="utf-8",
        )
        assert go_release._module_path(tmp_path) == "github.com/hyperi-io/x"

    def test_no_go_mod_is_none(self, tmp_path: Path) -> None:
        assert go_release._module_path(tmp_path) is None


def _npm_package(root: Path, markers: Path) -> None:
    """A package whose every publish-time script leaves a marker file."""
    scripts = {
        name: f"node -e \"require('fs').writeFileSync('{markers.as_posix()}/{name}','')\""
        for name in (
            "prepublishOnly",
            "prepack",
            "prepare",
            "postpack",
            "publish",
            "postpublish",
        )
    }
    (root / "package.json").write_text(
        json.dumps({"name": "hyperi-ci-probe", "version": "1.2.3", "scripts": scripts}),
        encoding="utf-8",
    )
    (root / "index.js").write_text("", encoding="utf-8")
    # Read only if npm runs inside the package; the upload must not.
    (root / ".npmrc").write_text("registry=https://probe.invalid/\n", encoding="utf-8")


def _npm_config(root: Path) -> Any:
    (root / ".hyperi-ci.yaml").write_text(
        yaml.safe_dump({"release": {"enabled": True}}), encoding="utf-8"
    )
    return load_config(reload=True, project_dir=root)


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
class TestNpmScriptsRunOnlyInPrepare:
    def test_prepare_runs_them_and_the_upload_runs_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        package = tmp_path / "pkg"
        markers = tmp_path / "markers"
        package.mkdir()
        markers.mkdir()
        _npm_package(package, markers)
        monkeypatch.chdir(package)
        out = tmp_path / "prepared"

        rc, facts = ts_release.prepare(_npm_config(package), out)

        assert rc == 0
        tarball = out / facts["tarball"]
        assert tarball.is_file()
        assert sorted(p.name for p in markers.iterdir()) == [
            "postpack",
            "prepack",
            "prepare",
            "prepublishOnly",
        ]

        for marker in markers.iterdir():
            marker.unlink()
        rc = ts_release._npm_publish(
            tarball,
            ["//registry.npmjs.org/:_authToken=fake-npm-token"],
            ["--dry-run", "--registry", ts_release.NPMJS_REGISTRY],
        )

        assert rc == 0
        assert list(markers.iterdir()) == []

    def test_the_upload_runs_outside_the_repo_with_scripts_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("NPM_TOKEN", "fake-npm-token")
        recorder = _Recorder()
        monkeypatch.setattr(ts_release, "run_cmd", recorder)
        tarball = tmp_path / "p.tgz"

        assert ts_release._publish_npm(tarball) == 0

        ((cmd, cwd, _env),) = recorder.calls
        assert cmd[:3] == ["npm", "publish", str(tarball)]
        assert "--ignore-scripts" in cmd
        assert "fake-npm-token" not in " ".join(cmd)
        assert cwd is not None and cwd != tmp_path
        assert not (tmp_path / ".npmrc").exists()
