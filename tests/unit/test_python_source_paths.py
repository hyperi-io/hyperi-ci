# Project:   HyperI CI
# File:      tests/unit/test_python_source_paths.py
# Purpose:   Tests for Python source-path detection and the tools that scan it
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Where a Python project's source lives, and every scan pointed at it.

A ``src/`` layout scans ``src/``. A flat layout scans each top-level directory
holding Python, and a repo with none skips the scan out loud rather than
pointing a tool at a directory that does not exist.
"""

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci import common
from hyperi_ci.config import CIConfig
from hyperi_ci.languages import quality_common
from hyperi_ci.languages.python import quality
from hyperi_ci.languages.python import test as py_test
from hyperi_ci.versions import tool_version


def _touch(root: Path, *files: str) -> None:
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")


def _sources(raw: dict[str, Any] | None = None) -> list[str]:
    return quality_common.get_python_source_paths(CIConfig(_raw=raw or {}))


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fresh working tree as cwd, with the once-per-process note reset."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    common._note_unmatched_excludes.cache_clear()
    yield project
    common._note_unmatched_excludes.cache_clear()


class TestDetection:
    def test_src_layout_is_src_alone(self, repo: Path) -> None:
        _touch(repo, "src/pkg/core.py", "scripts/tool.py", "tests/test_core.py")
        assert _sources() == ["src/"]

    def test_src_without_python_falls_through(self, repo: Path) -> None:
        (repo / "src").mkdir()
        (repo / "src" / ".gitkeep").write_text("", encoding="utf-8")
        (repo / "src" / "main.go").write_text("package main\n", encoding="utf-8")
        _touch(repo, "scripts/tool.py")
        assert _sources() == ["scripts/"]

    def test_src_with_python_only_in_a_cache_falls_through(self, repo: Path) -> None:
        _touch(repo, "src/__pycache__/stale.py", "scripts/tool.py")
        assert _sources() == ["scripts/"]

    def test_flat_layout_scripts_and_tests(self, repo: Path) -> None:
        _touch(repo, "scripts/tool.py", "tests/test_tool.py")
        assert _sources() == ["scripts/"]

    def test_nested_package_is_found_by_its_top_directory(self, repo: Path) -> None:
        _touch(repo, "pkg/sub/deeper/mod.py", "tests/test_mod.py")
        assert _sources() == ["pkg/"]

    def test_directories_without_python_drop_out(self, repo: Path) -> None:
        _touch(repo, "scripts/tool.py")
        (repo / "schemas").mkdir()
        (repo / "schemas" / "a.yaml").write_text("a: 1\n", encoding="utf-8")
        (repo / "empty").mkdir()
        assert _sources() == ["scripts/"]

    def test_nothing_found(self, repo: Path) -> None:
        _touch(repo, "tests/test_x.py", "setup.py", "conftest.py")
        assert _sources() == []

    def test_root_level_modules_are_not_source(self, repo: Path) -> None:
        _touch(repo, "setup.py", "noxfile.py", "scripts/tool.py")
        assert _sources() == ["scripts/"]

    def test_tooling_and_hidden_directories_are_ignored(self, repo: Path) -> None:
        _touch(
            repo,
            "scripts/tool.py",
            ".venv/lib/site.py",
            "venv/lib/site.py",
            "env/lib/site.py",
            ".hidden/x.py",
            "build/lib/x.py",
            "dist/x.py",
            "docs/conf.py",
            "node_modules/pkg/x.py",
            "__pycache__/x.py",
            "pkg.egg-info/x.py",
        )
        assert _sources() == ["scripts/"]

    def test_handler_excludes_are_honoured(self, repo: Path) -> None:
        _touch(repo, "scripts/tool.py", "vendor/lib.py", "third_party/lib.py")
        raw = {"quality": {"exclude_paths": ["third_party"]}}
        assert _sources(raw) == ["scripts/"]

    @pytest.mark.parametrize("spelling", ["checks/", "checks", "./checks/"])
    def test_configured_test_paths_are_excluded(
        self, repo: Path, spelling: str
    ) -> None:
        _touch(repo, "app/main.py", "checks/test_main.py")
        raw = {"quality": {"test_paths": [spelling]}}
        assert _sources(raw) == ["app/"]

    def test_python_only_under_an_ignored_subdirectory_is_not_source(
        self, repo: Path
    ) -> None:
        _touch(repo, "scripts/tool.py", "web/node_modules/pkg/x.py")
        assert _sources() == ["scripts/"]

    def test_order_is_sorted(self, repo: Path) -> None:
        _touch(repo, "zeta/z.py", "alpha/a.py", "mid/m.py")
        assert _sources() == ["alpha/", "mid/", "zeta/"]

    def test_every_path_returned_exists(self, repo: Path) -> None:
        _touch(repo, "zeta/z.py", "alpha/a.py", "tests/test_a.py")
        found = _sources()
        assert found
        assert all(Path(p).is_dir() for p in found)


def _recorder_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a uv and uvx on PATH that record their argv and exit 0."""
    calls = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = (
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"with open({str(calls)!r}, 'a', encoding='utf-8') as fh:\n"
        "    argv = [pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]\n"
        "    fh.write(json.dumps(argv) + '\\n')\n"
    )
    for name in ("uv", "uvx"):
        (bin_dir / name).write_text(recorder, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("HYPERCI_QUALITY_SKIP", raising=False)
    monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
    return calls


def _argvs(calls: Path) -> list[list[str]]:
    if not calls.exists():
        return []
    return [json.loads(line) for line in calls.read_text().splitlines()]


def _scan(argvs: list[list[str]], *marker: str) -> list[str]:
    """Return the one argv containing ``marker`` as a contiguous run."""
    width = len(marker)
    hits = [
        argv
        for argv in argvs
        if any(tuple(argv[i : i + width]) == marker for i in range(len(argv)))
    ]
    assert len(hits) == 1, hits
    return hits[0]


class TestQualityScansTheDetectedSource:
    @pytest.fixture
    def flat(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> list[list[str]]:
        _touch(repo, "scripts/tool.py", "pkg/sub/mod.py", "tests/test_tool.py")
        calls = _recorder_path(tmp_path, monkeypatch)
        assert quality.run(CIConfig(_raw={})) == 0
        return _argvs(calls)

    def test_ruff_security(self, flat: list[list[str]]) -> None:
        argv = _scan(flat, "--select", "S")
        assert argv[argv.index("--output-format=concise") + 1 :][:2] == [
            "pkg/",
            "scripts/",
        ]
        assert "src/" not in argv

    def test_ruff_docstrings(self, flat: list[list[str]]) -> None:
        argv = _scan(flat, "--select", "D")
        assert argv[argv.index("--output-format=concise") + 1 :][:2] == [
            "pkg/",
            "scripts/",
        ]
        assert "src/" not in argv

    def test_bandit(self, flat: list[list[str]]) -> None:
        argv = _scan(flat, "bandit", "-r")
        assert argv[argv.index("-r") + 1 :][:2] == ["pkg/", "scripts/"]
        assert "src/" not in argv

    def test_vulture(self, flat: list[list[str]]) -> None:
        argv = _scan(flat, "vulture")
        assert argv[argv.index("vulture") + 1 :][:2] == ["pkg/", "scripts/"]
        assert "src/" not in argv

    def test_bandit_excludes_the_default_test_path(self, flat: list[list[str]]) -> None:
        argv = _scan(flat, "bandit", "-r")
        assert argv[argv.index("--exclude") + 1] == "tests/"

    def test_bandit_excludes_every_configured_test_path(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _touch(repo, "app/main.py", "checks/test_a.py", "tests/test_b.py")
        calls = _recorder_path(tmp_path, monkeypatch)
        raw = {"quality": {"test_paths": ["checks/", "tests/"]}}
        assert quality.run(CIConfig(_raw=raw)) == 0
        argv = _scan(_argvs(calls), "bandit", "-r")
        assert argv[argv.index("--exclude") + 1] == "checks/,tests/"

    def test_bandit_carries_test_paths_and_quality_excludes_in_one_flag(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bandit's --exclude is action="store": a second flag drops the first."""
        _touch(repo, "app/main.py", "tests/test_a.py")
        (repo / "vendor").mkdir()
        calls = _recorder_path(tmp_path, monkeypatch)
        assert quality.run(CIConfig(_raw={})) == 0
        argv = _scan(_argvs(calls), "bandit", "-r")
        exclude_flags = [
            a for a in argv if a == "--exclude" or a.startswith("--exclude=")
        ]
        assert len(exclude_flags) == 1
        assert argv[argv.index("--exclude") + 1] == "tests/,*/vendor/*"

    def test_src_layout_keeps_src(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _touch(repo, "src/pkg/core.py", "scripts/tool.py", "tests/test_core.py")
        calls = _recorder_path(tmp_path, monkeypatch)
        assert quality.run(CIConfig(_raw={})) == 0
        argvs = _argvs(calls)
        for marker in (("--select", "S"), ("--select", "D"), ("-r",), ("vulture",)):
            argv = _scan(argvs, *marker)
            assert "src/" in argv
            assert "scripts/" not in argv


class TestQualityWithNoSource:
    def test_each_source_scan_is_skipped_out_loud(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _touch(repo, "tests/test_x.py", "setup.py")
        calls = _recorder_path(tmp_path, monkeypatch)
        warnings: list[str] = []
        downgrades: list[str] = []
        monkeypatch.setattr(quality, "warn", warnings.append)
        monkeypatch.setattr(
            quality_common,
            "announce",
            lambda msg, _title, **_kw: downgrades.append(msg),
        )

        assert quality.run(CIConfig(_raw={})) == 0

        argvs = _argvs(calls)
        assert not [a for a in argvs if "--select" in a]
        assert not [a for a in argvs if "bandit" in a or "vulture" in a]
        for tool in ("ruff security", "bandit", "ruff docstrings", "vulture"):
            assert any(
                w.startswith(f"  {tool}: skipped") and "no Python source" in w
                for w in warnings
            ), (tool, warnings)
        assert downgrades == []

    def test_a_disabled_tool_still_reads_as_disabled(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _touch(repo, "tests/test_x.py")
        _recorder_path(tmp_path, monkeypatch)
        warnings: list[str] = []
        infos: list[str] = []
        monkeypatch.setattr(quality, "warn", warnings.append)
        monkeypatch.setattr(quality_common, "warn", warnings.append)
        monkeypatch.setattr(quality_common, "info", infos.append)
        raw = {"quality": {"python": {"vulture": "disabled"}}}

        quality.run(CIConfig(_raw=raw))

        assert "  vulture: disabled" in infos
        assert not any(w.startswith("  vulture:") for w in warnings)


class _Pytest:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def stream(self, cmd: list[str], *, on_line: Any = None, **_kw: Any) -> Any:
        self.commands.append(cmd)
        return 0, "1 passed in 0.01s\n"


@pytest.fixture
def pytest_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> _Pytest:
    module = "hyperi_ci.languages.python.test"
    rec = _Pytest()
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setattr(f"{module}.shutil.which", lambda _tool: "/usr/bin/x")
    monkeypatch.setattr(f"{module}._resolve_cmd", lambda cmd: cmd)
    monkeypatch.setattr(f"{module}.stream_cmd", rec.stream)
    monkeypatch.setattr(f"{module}.announce_tier", lambda *_a: None)
    monkeypatch.setattr(f"{module}.is_ci", lambda: False)
    return rec


def _coverage_config(**test: Any) -> CIConfig:
    return CIConfig(
        _raw={"test": {"coverage": True, "python": {"parallel": False}, **test}}
    )


def _cov_args(command: list[str]) -> list[str]:
    return [a for a in command if a.startswith("--cov")]


class TestCoverageFollowsTheSource:
    def test_src_layout_is_unchanged(self, repo: Path, pytest_run: _Pytest) -> None:
        _touch(repo, "src/pkg/core.py", "tests/test_core.py")
        assert py_test.run(_coverage_config()) == 0
        assert _cov_args(pytest_run.commands[0]) == ["--cov=src", "--cov-report=xml"]

    def test_flat_layout_covers_each_directory(
        self, repo: Path, pytest_run: _Pytest
    ) -> None:
        _touch(repo, "scripts/tool.py", "pkg/mod.py", "tests/test_tool.py")
        assert py_test.run(_coverage_config()) == 0
        assert _cov_args(pytest_run.commands[0]) == [
            "--cov=pkg",
            "--cov=scripts",
            "--cov-report=xml",
        ]

    def test_a_project_cov_source_is_kept_alone(
        self, repo: Path, pytest_run: _Pytest
    ) -> None:
        """dfe-schemas: an untested scripts/ beside it would sink an 80 floor."""
        _touch(repo, "dfe_schemas/core.py", "scripts/tool.py", "tests/test_core.py")
        args = ["-v", "--tb=short", "--cov=dfe_schemas"]
        config = _coverage_config(min_coverage=80)
        config._raw["test"]["python"]["args"] = args
        assert py_test.run(config) == 0
        assert pytest_run.commands == [
            [
                "pytest",
                "-v",
                "--tb=short",
                "--cov=dfe_schemas",
                "--cov-report=xml",
                "--cov-fail-under=80",
                "--override-ini=tmp_path_retention_policy=failed",
                "-rfEs",
            ]
        ]

    def test_a_bare_cov_is_the_projects_choice_too(
        self, repo: Path, pytest_run: _Pytest
    ) -> None:
        _touch(repo, "pkg/core.py", "scripts/tool.py")
        config = _coverage_config()
        config._raw["test"]["python"]["args"] = ["--cov"]
        assert py_test.run(config) == 0
        assert _cov_args(pytest_run.commands[0]) == ["--cov", "--cov-report=xml"]

    def test_a_cov_in_addopts_is_the_projects_choice_too(
        self, repo: Path, pytest_run: _Pytest
    ) -> None:
        _touch(repo, "pkg/core.py", "scripts/tool.py")
        (repo / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = "--cov=pkg"\n', "utf-8"
        )
        assert py_test.run(_coverage_config()) == 0
        assert _cov_args(pytest_run.commands[0]) == ["--cov-report=xml"]

    def test_no_source_runs_without_coverage(
        self, repo: Path, pytest_run: _Pytest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _touch(repo, "tests/test_x.py")
        warnings: list[str] = []
        monkeypatch.setattr(py_test, "warn", warnings.append)
        assert py_test.run(_coverage_config(min_coverage=80)) == 0
        assert _cov_args(pytest_run.commands[0]) == []
        assert any("no Python source" in w for w in warnings)


class TestSrcLayoutIsByteIdentical:
    """A src/ layout gets exactly the command lines it got before detection."""

    def test_quality_argv(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _touch(repo, "src/pkg/core.py", "scripts/tool.py", "tests/test_core.py")
        (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", "utf-8")
        (repo / "vendor").mkdir()
        calls = _recorder_path(tmp_path, monkeypatch)
        assert quality.run(CIConfig(_raw={})) == 0
        argvs = _argvs(calls)
        bandit = f"bandit=={tool_version('bandit')}"
        vulture = f"vulture=={tool_version('vulture')}"
        # The project declares no Python, so the AST tools take the running one.
        python = f"{sys.version_info.major}.{sys.version_info.minor}"

        assert _scan(argvs, "bandit", "-r") == [
            "uvx", "--python", python, "--from", bandit, "bandit", "-r", "src/", "-ll",
            "-c", "pyproject.toml", "--exclude", "tests/,*/vendor/*",
        ]  # fmt: skip
        assert _scan(argvs, "--select", "S") == [
            "uv", "run", "ruff", "check", "--select", "S",
            "--output-format=concise", "src/", "--extend-exclude=vendor",
        ]  # fmt: skip
        assert _scan(argvs, "--select", "D") == [
            "uv", "run", "ruff", "check", "--select", "D",
            "--output-format=concise", "src/", "--extend-exclude=vendor",
        ]  # fmt: skip
        assert _scan(argvs, "vulture") == [
            "uvx", "--python", python, "--from", vulture, "vulture", "src/",
            "--exclude=*/vendor/*",
        ]  # fmt: skip

    def test_pytest_argv(self, repo: Path, pytest_run: _Pytest) -> None:
        _touch(repo, "src/pkg/core.py", "scripts/tool.py", "tests/test_core.py")
        assert py_test.run(_coverage_config(min_coverage=80)) == 0
        assert pytest_run.commands == [
            [
                "pytest",
                "-v",
                "--tb=short",
                "--cov=src",
                "--cov-report=xml",
                "--cov-fail-under=80",
                "--override-ini=tmp_path_retention_policy=failed",
                "-rfEs",
            ]
        ]
