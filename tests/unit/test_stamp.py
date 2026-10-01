# Project:   HyperI CI
# File:      tests/unit/test_stamp.py
# Purpose:   Tests for central version stamping (VERSION + manifest)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Central `stamp_version`: writes VERSION (language-agnostic), then
delegates the manifest stamp to the detected language."""

import shlex
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hyperi_ci import config as config_module
from hyperi_ci.cli import app
from hyperi_ci.config import load_config
from hyperi_ci.stamp import SKIP_STAMP_CMD_ENV, stamp_paths, stamp_version


@pytest.fixture(autouse=True)
def _restore_config_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp_version reloads config from tmp_path; keep that out of other tests."""
    monkeypatch.setattr(config_module, "_config_cache", None)


def _configure(root: Path, release: dict) -> None:
    (root / ".hyperi-ci.yaml").write_text(
        yaml.safe_dump({"release": release}), encoding="utf-8"
    )


# Copies VERSION to a generated file, the shape of dfe-engine's spec generator.
_COPY_VERSION = (
    "from pathlib import Path; "
    "Path('spec.json').write_text("
    "'{\"version\": \"' + Path('VERSION').read_text().strip() + '\"}')"
)


class TestStampCommand:
    """`release.stamp_cmd` runs after VERSION is written, from the repo root."""

    def test_a_string_command_sees_the_new_version(self, tmp_path: Path) -> None:
        command = shlex.join([sys.executable, "-c", _COPY_VERSION])
        _configure(tmp_path, {"stamp_cmd": command})
        assert stamp_version("1.4.0", project_dir=tmp_path) == 0
        assert (tmp_path / "spec.json").read_text() == '{"version": "1.4.0"}'

    def test_a_list_is_the_argv(self, tmp_path: Path) -> None:
        _configure(tmp_path, {"stamp_cmd": [sys.executable, "-c", _COPY_VERSION]})
        assert stamp_version("1.4.1", project_dir=tmp_path) == 0
        assert (tmp_path / "spec.json").read_text() == '{"version": "1.4.1"}'

    def test_a_failing_command_fails_the_stamp(self, tmp_path: Path) -> None:
        _configure(
            tmp_path, {"stamp_cmd": [sys.executable, "-c", "raise SystemExit(3)"]}
        )
        assert stamp_version("1.4.0", project_dir=tmp_path) == 1
        # VERSION is still written: the command runs after it, and needs it.
        assert (tmp_path / "VERSION").read_text() == "1.4.0\n"

    def test_a_missing_program_fails_the_stamp(self, tmp_path: Path) -> None:
        _configure(tmp_path, {"stamp_cmd": "no-such-program-hyperi-ci --flag"})
        assert stamp_version("1.4.0", project_dir=tmp_path) == 1

    def test_a_wrong_type_fails_the_stamp(self, tmp_path: Path) -> None:
        _configure(tmp_path, {"stamp_cmd": {"run": "generate"}})
        assert stamp_version("1.4.0", project_dir=tmp_path) == 1

    def test_no_shell_is_involved(self, tmp_path: Path) -> None:
        """A `;` is an argument, not a second command."""
        script = "import sys; open('args.txt', 'w').write(repr(sys.argv[1:]))"
        _configure(
            tmp_path,
            {"stamp_cmd": [sys.executable, "-c", script, ";", "touch", "pwned"]},
        )
        assert stamp_version("1.4.0", project_dir=tmp_path) == 0
        assert (tmp_path / "args.txt").read_text() == "[';', 'touch', 'pwned']"
        assert not (tmp_path / "pwned").exists()

    def test_unset_runs_nothing(self, tmp_path: Path) -> None:
        _configure(tmp_path, {"stamp_cmd": ""})
        assert stamp_version("1.4.0", project_dir=tmp_path) == 0


class TestSkippingTheStampCommand:
    """The Container job stamps the image's tree while holding registry logins.

    So it stamps with ``--no-stamp-cmd``: VERSION and the manifest are written,
    and the repo's own command does not run.
    """

    @pytest.fixture(autouse=True)
    def _no_inherited_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SKIP_STAMP_CMD_ENV, raising=False)

    def test_the_switch_writes_version_and_skips_the_command(
        self, tmp_path: Path
    ) -> None:
        _configure(tmp_path, {"stamp_cmd": [sys.executable, "-c", _COPY_VERSION]})
        assert stamp_version("1.4.0", project_dir=tmp_path, run_stamp_cmd=False) == 0
        assert (tmp_path / "VERSION").read_text() == "1.4.0\n"
        assert not (tmp_path / "spec.json").exists()

    def test_the_switch_still_stamps_the_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "0.0.1"\n', encoding="utf-8"
        )
        _configure(tmp_path, {"stamp_cmd": [sys.executable, "-c", _COPY_VERSION]})
        assert stamp_version("1.4.0", project_dir=tmp_path, run_stamp_cmd=False) == 0
        assert 'version = "1.4.0"' in (tmp_path / "pyproject.toml").read_text()
        assert not (tmp_path / "spec.json").exists()

    def test_a_malformed_command_does_not_fail_a_skipped_stamp(
        self, tmp_path: Path
    ) -> None:
        """The prepare job runs the command and reports a bad value."""
        _configure(tmp_path, {"stamp_cmd": {"run": "generate"}})
        assert stamp_version("1.4.0", project_dir=tmp_path, run_stamp_cmd=False) == 0

    @pytest.mark.parametrize(
        ("args", "env"),
        [
            (["--no-stamp-cmd"], {}),
            ([], {SKIP_STAMP_CMD_ENV: "1"}),
            ([], {SKIP_STAMP_CMD_ENV: "true"}),
        ],
    )
    def test_the_cli_skips_the_command(
        self, tmp_path: Path, args: list[str], env: dict[str, str]
    ) -> None:
        _configure(tmp_path, {"stamp_cmd": [sys.executable, "-c", _COPY_VERSION]})
        result = CliRunner().invoke(
            app, ["stamp-version", "1.4.0", "-C", str(tmp_path), *args], env=env
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "VERSION").read_text() == "1.4.0\n"
        assert not (tmp_path / "spec.json").exists()

    @pytest.mark.parametrize(
        "env",
        [{}, {SKIP_STAMP_CMD_ENV: "0"}, {SKIP_STAMP_CMD_ENV: "false"}],
    )
    def test_the_cli_runs_the_command_otherwise(
        self, tmp_path: Path, env: dict[str, str]
    ) -> None:
        _configure(tmp_path, {"stamp_cmd": [sys.executable, "-c", _COPY_VERSION]})
        result = CliRunner().invoke(
            app, ["stamp-version", "1.4.0", "-C", str(tmp_path)], env=env
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "spec.json").read_text() == '{"version": "1.4.0"}'


class TestStampPaths:
    """`release.stamp_paths` become paths in a commit to the default branch."""

    @staticmethod
    def _resolve(root: Path, paths: object) -> list[str]:
        _configure(root, {"stamp_paths": paths})
        return stamp_paths(load_config(project_dir=root, reload=True), root)

    def test_relative_paths_pass_through(self, tmp_path: Path) -> None:
        paths = ["openapi-spec/openapi.json", "openapi-spec/openapi.e2e.json"]
        assert self._resolve(tmp_path, paths) == paths

    def test_duplicates_and_backslashes_collapse(self, tmp_path: Path) -> None:
        paths = ["spec\\openapi.json", "spec/openapi.json", "./spec/openapi.json"]
        assert self._resolve(tmp_path, paths) == ["spec/openapi.json"]

    @pytest.mark.parametrize("bad", ["/etc/passwd", "../other/file", "spec/../../x"])
    def test_a_path_outside_the_repo_is_refused(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ValueError, match="relative to the repo root"):
            self._resolve(tmp_path, [bad])

    def test_a_symlink_out_of_the_repo_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "escape").symlink_to(tmp_path)
        with pytest.raises(ValueError, match="resolves outside the repo"):
            self._resolve(root, ["escape/secret"])

    def test_a_string_is_not_a_list(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            self._resolve(tmp_path, "openapi.json")

    def test_unset_is_empty(self, tmp_path: Path) -> None:
        assert self._resolve(tmp_path, []) == []


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
        # hatch-vcs / dynamic projects have no [project] version line -- never insert.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndynamic = ["version"]\n'
        )
        stamp_version("7.8.9", project_dir=tmp_path)
        txt = (tmp_path / "pyproject.toml").read_text()
        assert "version =" not in txt.split("[project]")[1]
        # VERSION file still carries the truth
        assert (tmp_path / "VERSION").read_text() == "7.8.9\n"


# The ci-test-python-lib layout: hatch reads the version from __init__.py.
_HATCH_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "hatch-lib"
dynamic = ["version"]

[tool.hatch.version]
path = "src/hatch_lib/__init__.py"
{extra}
[tool.hatch.build.targets.wheel]
packages = ["src/hatch_lib"]
"""

_HATCH_INIT = '"""A lib."""\n\n__version__ = "1.0.2"\n\n\ndef f() -> str:\n    return __version__\n'


def _hatch_project(root: Path, extra: str = "", init: str = _HATCH_INIT) -> Path:
    (root / "pyproject.toml").write_text(
        _HATCH_PYPROJECT.format(extra=extra), encoding="utf-8"
    )
    init_path = root / "src" / "hatch_lib" / "__init__.py"
    init_path.parent.mkdir(parents=True)
    init_path.write_text(init, encoding="utf-8")
    return init_path


class TestHatchDynamicVersionStamp:
    """A dynamic version read by hatch from a file gets that file stamped (#386)."""

    def test_the_default_pattern_stamps_dunder_version(self, tmp_path: Path) -> None:
        init = _hatch_project(tmp_path)
        assert stamp_version("1.0.6", project_dir=tmp_path) == 0
        assert init.read_text(encoding="utf-8") == _HATCH_INIT.replace(
            '"1.0.2"', '"1.0.6"'
        )

    @pytest.mark.parametrize(
        ("line", "stamped"),
        [
            ("__version__ = '1.0.2'", "__version__ = '1.0.6'"),
            ('__version__ = "v1.0.2"', '__version__ = "v1.0.6"'),
            ('VERSION = "1.0.2"', 'VERSION = "1.0.6"'),
        ],
    )
    def test_the_default_pattern_forms_hatch_reads(
        self, tmp_path: Path, line: str, stamped: str
    ) -> None:
        init = _hatch_project(tmp_path, init=f"{line}\n")
        assert stamp_version("1.0.6", project_dir=tmp_path) == 0
        assert init.read_text(encoding="utf-8") == f"{stamped}\n"

    def test_a_custom_pattern_is_honoured(self, tmp_path: Path) -> None:
        init = _hatch_project(
            tmp_path,
            extra="pattern = 'RELEASE: (?P<version>\\S+)'\n",
            init="# RELEASE: 1.0.2\n__version__ = '0.0.0'\n",
        )
        assert stamp_version("1.0.6", project_dir=tmp_path) == 0
        assert init.read_text(encoding="utf-8") == (
            "# RELEASE: 1.0.6\n__version__ = '0.0.0'\n"
        )

    def test_no_match_fails_the_stamp(self, tmp_path: Path) -> None:
        _hatch_project(tmp_path, init="def f() -> None:\n    pass\n")
        assert stamp_version("1.0.6", project_dir=tmp_path) == 1

    def test_a_missing_version_file_fails_the_stamp(self, tmp_path: Path) -> None:
        _hatch_project(tmp_path).unlink()
        assert stamp_version("1.0.6", project_dir=tmp_path) == 1

    def test_a_pattern_without_a_version_group_fails_the_stamp(
        self, tmp_path: Path
    ) -> None:
        _hatch_project(tmp_path, extra="pattern = '__version__'\n")
        assert stamp_version("1.0.6", project_dir=tmp_path) == 1

    def test_an_unknown_source_fails_the_stamp(self, tmp_path: Path) -> None:
        _hatch_project(tmp_path, extra='source = "env"\nvariable = "V"\n')
        assert stamp_version("1.0.6", project_dir=tmp_path) == 1

    @pytest.mark.parametrize("source", ["vcs", "code"])
    def test_vcs_and_code_sources_are_left_to_the_backend(
        self, tmp_path: Path, source: str
    ) -> None:
        """hatch-vcs reads git and hyperi-ci's own `code` source reads VERSION."""
        init = _hatch_project(
            tmp_path, extra=f'source = "{source}"\nexpression = "__version__"\n'
        )
        assert stamp_version("1.0.6", project_dir=tmp_path) == 0
        assert init.read_text(encoding="utf-8") == _HATCH_INIT

    def test_a_static_version_ignores_a_stray_hatch_table(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.0.0"\n\n'
            '[tool.hatch.version]\npath = "missing.py"\n',
            encoding="utf-8",
        )
        assert stamp_version("1.0.6", project_dir=tmp_path) == 0


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
        # Go versions via ldflags from the VERSION file -- no manifest field.
        (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.23\n")
        rc = stamp_version("2.2.2", project_dir=tmp_path)
        assert rc == 0
        assert (tmp_path / "VERSION").read_text() == "2.2.2\n"
        # go.mod untouched
        assert "2.2.2" not in (tmp_path / "go.mod").read_text()
