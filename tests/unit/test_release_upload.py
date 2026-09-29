# Project:   HyperI CI
# File:      tests/unit/test_release_upload.py
# Purpose:   The upload runs no repo code and trusts nothing prepare handed it
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The upload half of issue #409's split, per ecosystem.

Each test pins how an upload avoids the repo's code and config while it holds
a publish credential, or how it refuses an input the prepare job could have
forged.
"""

import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from hyperi_ci import config as config_module
from hyperi_ci.config import load_config
from hyperi_ci.languages.golang import release as go_release
from hyperi_ci.languages.python import release as py_release
from hyperi_ci.languages.rust import release as rust_release
from hyperi_ci.languages.typescript import release as ts_release
from hyperi_ci.release_prepare import MANIFEST_NAME, PREPARED_ENV


@pytest.fixture(autouse=True)
def _fresh_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_config_cache", None)
    monkeypatch.delenv(PREPARED_ENV, raising=False)


def _write_prepared(root: Path, **overrides: Any) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 2, "version": "1.2.3", "language": "rust", "facts": {}}
    manifest.update(overrides)
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return root


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Recorder:
    """Stands in for run_cmd, records argv and working directory, answers cargo metadata."""

    def __init__(self, *, bins: list[str] | None = None, metadata_rc: int = 0) -> None:
        self.calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []
        self.bins = bins or []
        self.metadata_rc = metadata_rc

    def __call__(self, cmd: list[str], **kwargs: Any) -> _Result:
        cwd = kwargs.get("cwd")
        self.calls.append((cmd, Path(cwd) if cwd else None, kwargs.get("env")))
        if cmd[:2] == ["cargo", "metadata"]:
            if self.metadata_rc:
                return _Result(self.metadata_rc, stderr="error: failed to parse")
            targets = [{"name": "c", "kind": ["lib"]}]
            targets += [{"name": b, "kind": ["bin"]} for b in self.bins]
            return _Result(stdout=json.dumps({"packages": [{"targets": targets}]}))
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
        monkeypatch.setattr(rust_release.semver_checks, "run", _forbidden, raising=True)
        return root

    def test_every_cargo_call_runs_outside_the_crate(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inside it, rustup reads rust-toolchain.toml and cargo .cargo/config.toml."""
        monkeypatch.setenv(
            PREPARED_ENV, str(_write_prepared(tmp_path / "p", facts={"crate": True}))
        )
        recorder = _Recorder()
        monkeypatch.setattr(rust_release, "run_cmd", recorder)

        assert rust_release.run(_rust_config(crate)) == 0

        assert [c[0][:2] for c in recorder.calls] == [
            ["cargo", "metadata"],
            ["cargo", "publish"],
        ]
        for cmd, cwd, _env in recorder.calls:
            assert cmd[cmd.index("--manifest-path") + 1] == str(crate / "Cargo.toml")
            assert cwd is not None and not cwd.is_relative_to(crate)
            assert "fake-cargo-token" not in cmd
        assert "--no-verify" in recorder.calls[1][0]
        assert 'version = "1.2.3"' in (crate / "Cargo.toml").read_text(encoding="utf-8")

    def test_a_forged_crate_fact_cannot_publish_a_binary_app(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            PREPARED_ENV, str(_write_prepared(tmp_path / "p", facts={"crate": True}))
        )
        recorder = _Recorder(bins=["app"])
        monkeypatch.setattr(rust_release, "run_cmd", recorder)

        assert rust_release.run(_rust_config(crate)) == 0
        assert [c[0][:2] for c in recorder.calls] == [["cargo", "metadata"]]

    def test_a_library_prepare_did_not_check_is_refused(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            PREPARED_ENV, str(_write_prepared(tmp_path / "p", facts={"crate": False}))
        )
        recorder = _Recorder()
        monkeypatch.setattr(rust_release, "run_cmd", recorder)

        assert rust_release.run(_rust_config(crate)) == 1
        assert ["cargo", "publish"] not in [c[0][:2] for c in recorder.calls]

    def test_prepare_runs_the_checks_and_packages(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Recorder()
        checked: list[bool] = []
        monkeypatch.setattr(rust_release, "run_cmd", recorder)
        monkeypatch.setattr(
            rust_release.semver_checks, "run", lambda _c: checked.append(True) or 0
        )

        rc, facts = rust_release.prepare(_rust_config(crate), tmp_path / "out")

        assert (rc, facts) == (0, {"crate": True})
        assert checked == [True]
        assert [c[0][:2] for c in recorder.calls] == [
            ["cargo", "metadata"],
            ["cargo", "package"],
        ]

    def test_prepare_says_a_binary_app_has_no_crate(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rust_release, "run_cmd", _Recorder(bins=["app"]))
        rc, facts = rust_release.prepare(_rust_config(crate), tmp_path / "out")
        assert (rc, facts) == (0, {"crate": False})

    def test_a_cargo_metadata_failure_fails_prepare(
        self, crate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read as "binary app", a library crate went silently unpublished."""
        monkeypatch.setattr(rust_release, "run_cmd", _Recorder(metadata_rc=101))
        rc, _facts = rust_release.prepare(_rust_config(crate), tmp_path / "out")
        assert rc == 1


def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the upload must not run this")


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


def _tarball(path: Path, manifest: dict[str, Any]) -> Path:
    data = json.dumps(manifest).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return path


class TestNpmTarballIsChecked:
    """The tarball was packed by a job that ran repo code."""

    @pytest.fixture
    def checkout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        (tmp_path / "package.json").write_text('{"name": "p"}', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.3")
        return tmp_path

    def test_the_matching_package_passes(self, checkout: Path) -> None:
        tgz = _tarball(checkout / "t.tgz", {"name": "p", "version": "1.2.3"})
        assert ts_release._tarball_problem(tgz, ts_release.NPMJS_REGISTRY) is None

    @pytest.mark.parametrize(
        ("manifest", "reason"),
        [
            ({"name": "other", "version": "1.2.3"}, "'other'"),
            ({"name": "p", "version": "9.9.9"}, "version"),
            (
                {
                    "name": "p",
                    "version": "1.2.3",
                    "publishConfig": {"registry": "https://evil.example/"},
                },
                "publishConfig",
            ),
        ],
    )
    def test_a_mismatch_is_refused(
        self, checkout: Path, manifest: dict[str, Any], reason: str
    ) -> None:
        tgz = _tarball(checkout / "t.tgz", manifest)
        problem = ts_release._tarball_problem(tgz, ts_release.NPMJS_REGISTRY)
        assert problem is not None and reason in problem

    def test_a_refused_tarball_is_never_uploaded(
        self, checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tgz = _tarball(checkout / "t.tgz", {"name": "other", "version": "1.2.3"})
        recorder = _Recorder()
        monkeypatch.setattr(ts_release, "run_cmd", recorder)
        assert ts_release._publish(["npmjs"], tgz) == 1
        assert recorder.calls == []


class TestNpmUploadArgv:
    @pytest.mark.parametrize(
        ("publish", "registry", "token_env"),
        [
            ("_publish_npm", ts_release.NPMJS_REGISTRY, "NPM_TOKEN"),
            ("_publish_ghcr_npm", ts_release.GITHUB_NPM_REGISTRY, "GH_TOKEN"),
        ],
    )
    def test_outside_the_repo_scripts_off_and_registry_named(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        publish: str,
        registry: str,
        token_env: str,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(token_env, "fake-npm-token")
        recorder = _Recorder()
        monkeypatch.setattr(ts_release, "run_cmd", recorder)
        tarball = tmp_path / "p.tgz"

        assert getattr(ts_release, publish)(tarball) == 0

        ((cmd, cwd, _env),) = recorder.calls
        assert cmd[:3] == ["npm", "publish", str(tarball)]
        assert "--ignore-scripts" in cmd
        assert cmd[cmd.index("--registry") + 1] == registry
        assert "fake-npm-token" not in " ".join(cmd)
        assert cwd is not None and cwd != tmp_path
        assert not (tmp_path / ".npmrc").exists()


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


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_prepare_runs_the_packages_scripts_and_a_tarball_publish_runs_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npm runs no lifecycle script for a tarball, flag or not.

    So this proves the tarball is the boundary, and ``--ignore-scripts`` is a
    second one; the argv test above pins the flag.
    """
    package = tmp_path / "pkg"
    markers = tmp_path / "markers"
    package.mkdir()
    markers.mkdir()
    _npm_package(package, markers)
    monkeypatch.chdir(package)
    (package / ".hyperi-ci.yaml").write_text(
        yaml.safe_dump({"release": {"enabled": True}}), encoding="utf-8"
    )
    out = tmp_path / "prepared"

    rc, facts = ts_release.prepare(load_config(reload=True, project_dir=package), out)

    assert rc == 0
    tarball = out / facts["tarball"]
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
