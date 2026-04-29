# Project:   HyperI CI
# File:      tests/unit/test_container_detect.py
# Purpose:   Tests for containerisable artefact detection
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperi_ci.container.detect import detect


def _write_cargo_toml(project: Path, body: str) -> None:
    (project / "Cargo.toml").write_text(body)


def _write_pyproject(project: Path, body: str) -> None:
    (project / "pyproject.toml").write_text(body)


def _write_package_json(project: Path, body: dict) -> None:
    (project / "package.json").write_text(json.dumps(body))


# --- Rust ----------------------------------------------------------------


def test_rust_library_only_skips(tmp_path: Path) -> None:
    _write_cargo_toml(
        tmp_path,
        '[package]\nname = "mylib"\nversion = "0.1.0"\n[lib]\n',
    )
    decision = detect(language="rust", project_dir=tmp_path)
    assert decision.build is False
    assert "library-only" in decision.reason


def test_rust_binary_with_dockerfile_uses_custom_mode(tmp_path: Path) -> None:
    _write_cargo_toml(
        tmp_path,
        '[package]\nname = "myapp"\nversion = "0.1.0"\n',
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    decision = detect(language="rust", project_dir=tmp_path)
    assert decision.build is True
    assert decision.mode == "custom"


def test_rust_binary_with_rustlib_dep_uses_contract_mode(tmp_path: Path) -> None:
    _write_cargo_toml(
        tmp_path,
        '[package]\nname = "myapp"\nversion = "0.1.0"\n'
        '[dependencies]\nhyperi-rustlib = "2.5"\n',
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    decision = detect(language="rust", project_dir=tmp_path)
    assert decision.build is True
    assert decision.mode == "contract"


def test_rust_binary_no_dockerfile_no_rustlib_skips(tmp_path: Path) -> None:
    _write_cargo_toml(
        tmp_path,
        '[package]\nname = "myapp"\nversion = "0.1.0"\n',
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    decision = detect(language="rust", project_dir=tmp_path)
    assert decision.build is False
    assert "no container artefact" in decision.reason


def test_rust_workspace_with_bin_target(tmp_path: Path) -> None:
    _write_cargo_toml(
        tmp_path,
        '[package]\nname = "myapp"\nversion = "0.1.0"\n'
        '[[bin]]\nname = "myapp"\npath = "src/cli.rs"\n',
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.rs").write_text("fn main() {}\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    decision = detect(language="rust", project_dir=tmp_path)
    assert decision.build is True
    assert decision.mode == "custom"


# --- Python --------------------------------------------------------------


def test_python_library_only_skips(tmp_path: Path) -> None:
    _write_pyproject(
        tmp_path,
        '[project]\nname = "mylib"\nversion = "0.1.0"\n',
    )
    decision = detect(language="python", project_dir=tmp_path)
    assert decision.build is False


def test_python_with_console_script_and_dockerfile(tmp_path: Path) -> None:
    _write_pyproject(
        tmp_path,
        '[project]\nname = "myapp"\nversion = "0.1.0"\n'
        '[project.scripts]\nmyapp = "myapp.cli:main"\n',
    )
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    decision = detect(language="python", project_dir=tmp_path)
    assert decision.build is True
    assert decision.mode == "custom"


def test_python_with_console_script_no_dockerfile_uses_template(tmp_path: Path) -> None:
    _write_pyproject(
        tmp_path,
        '[project]\nname = "myapp"\nversion = "0.1.0"\n'
        '[project.scripts]\nmyapp = "myapp.cli:main"\n',
    )
    decision = detect(language="python", project_dir=tmp_path)
    assert decision.build is True
    assert decision.mode == "template"


# --- TypeScript ----------------------------------------------------------


def test_typescript_library_only_skips(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path,
        {"name": "mylib", "version": "0.1.0", "main": "dist/lib.js"},
    )
    # main field doesn't match server/main/index heuristic in the
    # detector — explicit lib name path
    decision = detect(language="typescript", project_dir=tmp_path)
    assert decision.build is False


def test_typescript_with_bin_uses_template(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path,
        {"name": "mycli", "version": "0.1.0", "bin": {"mycli": "dist/cli.js"}},
    )
    decision = detect(language="typescript", project_dir=tmp_path)
    assert decision.build is True
    assert decision.mode == "template"


def test_typescript_with_start_script_uses_template(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path,
        {
            "name": "myserver",
            "version": "0.1.0",
            "scripts": {"start": "node dist/server.js"},
        },
    )
    decision = detect(language="typescript", project_dir=tmp_path)
    assert decision.build is True


# --- Go ------------------------------------------------------------------


def test_go_with_main_package_and_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/myapp\ngo 1.22\n")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")
    (tmp_path / "Dockerfile").write_text("FROM golang:1.22-alpine\n")
    decision = detect(language="golang", project_dir=tmp_path)
    assert decision.build is True
    assert decision.mode == "custom"


def test_go_library_only_skips(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/mylib\ngo 1.22\n")
    (tmp_path / "lib.go").write_text("package mylib\n")
    decision = detect(language="golang", project_dir=tmp_path)
    assert decision.build is False


# --- Unknown languages ---------------------------------------------------


def test_unknown_language_with_dockerfile_uses_custom(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    decision = detect(language="bash", project_dir=tmp_path)
    assert decision.build is True
    assert decision.mode == "custom"


def test_unknown_language_without_dockerfile_skips(tmp_path: Path) -> None:
    decision = detect(language="bash", project_dir=tmp_path)
    assert decision.build is False


# --- Custom dockerfile path ---------------------------------------------


def test_custom_dockerfile_path_honoured(tmp_path: Path) -> None:
    _write_cargo_toml(
        tmp_path,
        '[package]\nname = "myapp"\nversion = "0.1.0"\n',
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "container").mkdir()
    (tmp_path / "container" / "Dockerfile.runtime").write_text("FROM scratch\n")

    decision = detect(
        language="rust",
        project_dir=tmp_path,
        dockerfile="container/Dockerfile.runtime",
    )
    assert decision.build is True
    assert decision.mode == "custom"


@pytest.mark.parametrize("dockerfile_name", ["Dockerfile", "Containerfile.app"])
def test_dockerfile_at_named_path(tmp_path: Path, dockerfile_name: str) -> None:
    _write_cargo_toml(
        tmp_path,
        '[package]\nname = "myapp"\nversion = "0.1.0"\n',
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / dockerfile_name).write_text("FROM scratch\n")

    decision = detect(language="rust", project_dir=tmp_path, dockerfile=dockerfile_name)
    assert decision.build is True
    assert decision.mode == "custom"
