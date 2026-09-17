# Project:   HyperI CI
# File:      tests/unit/test_python_version.py
# Purpose:   The project's own declaration decides its Python version
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""issue #150: CI must build a project on the interpreter it declares.

The fleet default is what a project gets when it declares nothing, not what
every project is held to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci import python_version

FLEET_DEFAULT = "3.14"


def _write(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8")


class TestRequiresPythonFloor:
    """The floor is the point: testing above it hides the bug it catches."""

    @pytest.mark.parametrize(
        "spec,expected",
        [
            (">=3.12", "3.12"),
            (">=3.12,<4.0", "3.12"),
            ("~=3.12", "3.12"),
            ("==3.12.*", "3.12"),
            (">=3.9,<3.13", "3.9"),
            # Several lower bounds: the lowest is the oldest interpreter the
            # project promises, so it is the one CI has to prove.
            (">=3.10,>=3.12", "3.10"),
        ],
    )
    def test_reads_the_lower_bound(
        self, tmp_path: Path, spec: str, expected: str
    ) -> None:
        _write(tmp_path, "pyproject.toml", f'[project]\nrequires-python = "{spec}"\n')
        assert python_version.requires_python_floor(tmp_path) == expected

    @pytest.mark.parametrize("spec", ["<4.0", "!=3.11", ">3.11"])
    def test_ignores_specifiers_that_name_no_floor(
        self, tmp_path: Path, spec: str
    ) -> None:
        # `>3.11` excludes a version rather than naming the first allowed one,
        # so reading it as a floor would test 3.11 -- a version the project
        # explicitly ruled out.
        _write(tmp_path, "pyproject.toml", f'[project]\nrequires-python = "{spec}"\n')
        assert python_version.requires_python_floor(tmp_path) is None

    def test_no_pyproject_is_not_an_error(self, tmp_path: Path) -> None:
        assert python_version.requires_python_floor(tmp_path) is None

    def test_malformed_pyproject_is_not_an_error(self, tmp_path: Path) -> None:
        # A broken manifest must not take the whole plan job down -- the
        # fallback names a usable interpreter instead.
        _write(tmp_path, "pyproject.toml", "[project\nrequires-python =")
        assert python_version.requires_python_floor(tmp_path) is None


class TestPeggedFile:
    """A `.python-version` file is someone writing the version down."""

    @pytest.mark.parametrize(
        "content,expected",
        [
            ("3.12\n", "3.12"),
            ("3.12.7\n", "3.12"),
            ("cpython-3.12\n", "3.12"),
            ("pypy@3.10\n", "3.10"),
            ("# a comment\n\n3.11\n", "3.11"),
        ],
    )
    def test_reads_the_peg(self, tmp_path: Path, content: str, expected: str) -> None:
        _write(tmp_path, ".python-version", content)
        assert python_version.pegged_version(tmp_path) == expected

    def test_absent_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert python_version.pegged_version(tmp_path) is None


class TestResolutionOrder:
    """Project first, then the caller, then the fleet."""

    def test_peg_beats_the_floor(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml", '[project]\nrequires-python = ">=3.12"\n')
        _write(tmp_path, ".python-version", "3.13\n")
        assert python_version.resolve(tmp_path, default=FLEET_DEFAULT) == (
            "3.13",
            ".python-version",
        )

    def test_floor_beats_the_fleet_default(self, tmp_path: Path) -> None:
        # The whole of issue #150: this repo used to get 3.14.
        _write(tmp_path, "pyproject.toml", '[project]\nrequires-python = ">=3.12"\n')
        assert python_version.resolve(tmp_path, default=FLEET_DEFAULT) == (
            "3.12",
            "requires-python",
        )

    def test_floor_beats_an_explicit_request(self, tmp_path: Path) -> None:
        # A repo that states its floor has answered the question. A caller
        # overriding it is how the floor silently stops being tested.
        _write(tmp_path, "pyproject.toml", '[project]\nrequires-python = ">=3.12"\n')
        assert python_version.resolve(
            tmp_path, requested="3.14", default=FLEET_DEFAULT
        ) == ("3.12", "requires-python")

    def test_request_is_used_when_the_project_declares_nothing(
        self, tmp_path: Path
    ) -> None:
        assert python_version.resolve(
            tmp_path, requested="3.13", default=FLEET_DEFAULT
        ) == ("3.13", "requested")

    def test_fleet_default_covers_a_project_that_declares_nothing(
        self, tmp_path: Path
    ) -> None:
        # A Rust or Go repo: the interpreter runs the CLI, nothing more.
        assert python_version.resolve(tmp_path, default=FLEET_DEFAULT) == (
            FLEET_DEFAULT,
            "default",
        )

    def test_nothing_at_all_resolves_to_empty(self, tmp_path: Path) -> None:
        # The caller must leave the interpreter choice alone rather than
        # install a version nobody named.
        assert python_version.resolve(tmp_path) == ("", "unresolved")

    def test_a_dynamic_version_project_still_resolves_its_floor(
        self, tmp_path: Path
    ) -> None:
        # hyperi-ci's own shape: dynamic version, declared floor.
        _write(
            tmp_path,
            "pyproject.toml",
            '[project]\ndynamic = ["version"]\nrequires-python = ">=3.14"\n',
        )
        assert python_version.resolve(tmp_path, default="3.12") == (
            "3.14",
            "requires-python",
        )


class TestTheRealFixtures:
    """The fleet fixture that proved the bug must resolve to its own floor."""

    def test_a_declared_floor_is_never_the_fleet_default(self, tmp_path: Path) -> None:
        # ci-test-python-app declares >=3.12 and passes no python-version, so
        # before #150 it was built and tested on 3.14.
        _write(tmp_path, "pyproject.toml", '[project]\nrequires-python = ">=3.12"\n')
        version, source = python_version.resolve(tmp_path, default=FLEET_DEFAULT)
        assert version != FLEET_DEFAULT
        assert (version, source) == ("3.12", "requires-python")
