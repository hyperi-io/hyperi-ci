# Project:   HyperI CI
# File:      tests/unit/deployment/test_manifest.py
# Purpose:   Shared Cargo.toml / pyproject.toml reader tests
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.deployment.manifest`.

These readers decide which producer a CI run dispatches, so a wrong
answer here is a wrong Dockerfile (or a silently skipped stage) in
every consumer repo. The workspace and comment-handling cases below
are the ones that bite in real manifests.
"""

from __future__ import annotations

from pathlib import Path

from hyperi_ci.deployment.manifest import (
    dep_features,
    extract_bin_names,
    extract_package_name,
    extract_workspace_members,
    manifest_self_name,
    produces_rust_binary,
    python_entry_point,
    resolve_workspace_members,
    rust_binary_name,
)


class TestProducesRustBinary:
    """Does cargo build a binary here? Drives Tier 1 detection."""

    def test_no_cargo_toml(self, tmp_path: Path) -> None:
        assert not produces_rust_binary(tmp_path)

    def test_lib_only_crate(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\n[lib]\n', encoding="utf-8"
        )
        assert not produces_rust_binary(tmp_path)

    def test_explicit_bin_table(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\n[[bin]]\nname = "demo"\npath = "src/main.rs"\n',
            encoding="utf-8",
        )
        assert produces_rust_binary(tmp_path)

    def test_bin_table_without_name_field(self, tmp_path: Path) -> None:
        # cargo defaults an unnamed [[bin]] to the package name, so the
        # table alone is the signal — don't require a name.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\n[[bin]]\npath = "src/main.rs"\n',
            encoding="utf-8",
        )
        assert produces_rust_binary(tmp_path)

    def test_implicit_src_main_rs(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\n', encoding="utf-8"
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        assert produces_rust_binary(tmp_path)

    def test_implicit_src_bin_flat(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\n', encoding="utf-8"
        )
        (tmp_path / "src" / "bin").mkdir(parents=True)
        (tmp_path / "src" / "bin" / "tool.rs").write_text(
            "fn main() {}\n", encoding="utf-8"
        )
        assert produces_rust_binary(tmp_path)

    def test_implicit_src_bin_nested_main(self, tmp_path: Path) -> None:
        # cargo also discovers src/bin/<name>/main.rs.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\n', encoding="utf-8"
        )
        (tmp_path / "src" / "bin" / "tool").mkdir(parents=True)
        (tmp_path / "src" / "bin" / "tool" / "main.rs").write_text(
            "fn main() {}\n", encoding="utf-8"
        )
        assert produces_rust_binary(tmp_path)

    def test_src_bin_with_no_rust_files(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\n[lib]\n', encoding="utf-8"
        )
        (tmp_path / "src" / "bin").mkdir(parents=True)
        (tmp_path / "src" / "bin" / "README.md").write_text("notes\n", encoding="utf-8")
        assert not produces_rust_binary(tmp_path)

    def test_workspace_member_with_binary(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/app", "crates/core"]\n',
            encoding="utf-8",
        )
        _write_member(tmp_path / "crates" / "app", "app", binary=True)
        _write_member(tmp_path / "crates" / "core", "core", binary=False)
        assert produces_rust_binary(tmp_path)

    def test_workspace_all_libs(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/core"]\n', encoding="utf-8"
        )
        _write_member(tmp_path / "crates" / "core", "core", binary=False)
        assert not produces_rust_binary(tmp_path)

    def test_glob_workspace_members(self, tmp_path: Path) -> None:
        # `members = ["crates/*"]` is idiomatic cargo. Treating the glob
        # as a literal path finds no member, so a real producer reads as
        # a library and SILENTLY stops emitting artefacts.
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/*"]\n', encoding="utf-8"
        )
        _write_member(tmp_path / "crates" / "app", "app", binary=True)
        assert produces_rust_binary(tmp_path)

    def test_glob_workspace_all_libs(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/*"]\n', encoding="utf-8"
        )
        _write_member(tmp_path / "crates" / "core", "core", binary=False)
        assert not produces_rust_binary(tmp_path)

    def test_self_referential_member_terminates(self, tmp_path: Path) -> None:
        # `members = ["."]` points the workspace at itself. Cargo would
        # reject it at build time, but tier detection must not recurse
        # until the stack blows.
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["."]\n', encoding="utf-8"
        )
        assert not produces_rust_binary(tmp_path)

    def test_mutually_recursive_members_terminate(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["a"]\n', encoding="utf-8"
        )
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "Cargo.toml").write_text(
            '[workspace]\nmembers = [".."]\n', encoding="utf-8"
        )
        assert not produces_rust_binary(tmp_path)


class TestResolveWorkspaceMembers:
    """Glob expansion, with non-crate matches filtered out."""

    def test_literal_members(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/app"]\n', encoding="utf-8"
        )
        _write_member(tmp_path / "crates" / "app", "app", binary=True)
        text = (tmp_path / "Cargo.toml").read_text(encoding="utf-8")
        assert resolve_workspace_members(tmp_path, text) == [
            tmp_path / "crates" / "app"
        ]

    def test_glob_skips_dirs_without_a_manifest(self, tmp_path: Path) -> None:
        # A docs/ or fixtures/ dir sitting alongside the crates must not
        # come back as a member.
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/*"]\n', encoding="utf-8"
        )
        _write_member(tmp_path / "crates" / "app", "app", binary=True)
        (tmp_path / "crates" / "notes").mkdir()
        text = (tmp_path / "Cargo.toml").read_text(encoding="utf-8")
        assert resolve_workspace_members(tmp_path, text) == [
            tmp_path / "crates" / "app"
        ]

    def test_missing_literal_member_dropped(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/gone"]\n', encoding="utf-8"
        )
        text = (tmp_path / "Cargo.toml").read_text(encoding="utf-8")
        assert resolve_workspace_members(tmp_path, text) == []


class TestExtractNames:
    """Name extraction has to survive real-world manifest formatting."""

    def test_package_name(self) -> None:
        assert extract_package_name('[package]\nname = "dfe-loader"\n') == "dfe-loader"

    def test_package_name_with_inline_comment(self) -> None:
        # `.strip('"')` alone leaves the comment glued on, which yields a
        # binary name that resolves to nothing.
        text = '[package]\nname = "dfe-loader"  # the app itself\n'
        assert extract_package_name(text) == "dfe-loader"

    def test_package_name_single_quoted(self) -> None:
        assert extract_package_name("[package]\nname = 'demo'\n") == "demo"

    def test_no_package_table(self) -> None:
        assert extract_package_name('[workspace]\nmembers = ["a"]\n') is None

    def test_bin_names_in_order(self) -> None:
        text = (
            '[[bin]]\nname = "pgo-driver"\npath = "src/bin/pgo.rs"\n'
            '[[bin]]\nname = "dfe-receiver"\npath = "src/main.rs"\n'
        )
        assert extract_bin_names(text) == ["pgo-driver", "dfe-receiver"]

    def test_bin_name_with_inline_comment(self) -> None:
        text = '[[bin]]\nname = "demo"  # main entry\n'
        assert extract_bin_names(text) == ["demo"]

    def test_self_name_ignores_other_sections(self) -> None:
        text = '[features.weird]\nname = "scalo"\n'
        assert manifest_self_name(text) is None

    def test_workspace_members_multiline(self) -> None:
        text = '[workspace]\nmembers = [\n    "a",\n    "b",\n]\n'
        assert extract_workspace_members(text) == ["a", "b"]


class TestRustBinaryName:
    """Cycle safety for the name resolver that shares the workspace walk."""

    def test_self_referential_member_terminates(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["."]\n', encoding="utf-8"
        )
        assert rust_binary_name(tmp_path) is None

    def test_glob_member_resolved(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/*"]\n', encoding="utf-8"
        )
        _write_member(tmp_path / "crates" / "app", "app", binary=True)
        assert rust_binary_name(tmp_path) == "app"


class TestDepFeatures:
    """Cargo feature extraction. None means unknown, not "no features"."""

    def test_absent_dep(self) -> None:
        assert dep_features('[dependencies]\nserde = "1"\n', "scalo") is None

    def test_plain_version_string_is_known_empty(self) -> None:
        # Default features only, which do not include deployment.
        assert dep_features('[dependencies]\nscalo = "2.9"\n', "scalo") == frozenset()

    def test_inline_table_features(self) -> None:
        text = '[dependencies]\nscalo = { version = "2.9", features = ["cli", "deployment"] }\n'
        assert dep_features(text, "scalo") == frozenset({"cli", "deployment"})

    def test_multiline_features_array(self) -> None:
        # dfe-archiver's shape: the array wraps across lines.
        text = (
            "[dependencies]\n"
            "scalo = { workspace = true, features = [\n"
            '    "config", "logger",\n'
            '    "deployment", "cli-service",\n'
            "] }\n"
        )
        assert dep_features(text, "scalo") == frozenset(
            {"config", "logger", "deployment", "cli-service"}
        )

    def test_workspace_inheritance_without_features_is_unknown(self) -> None:
        # The real list lives in the workspace root, so this manifest
        # genuinely cannot answer.
        text = "[dependencies]\nscalo.workspace = true\n"
        assert dep_features(text, "scalo") is None

    def test_workspace_true_with_features_is_known(self) -> None:
        # `workspace = true` here inherits the VERSION; the features are
        # stated outright. Reading this as unknown made hyperi-ci demote
        # dfe-archiver, a real producer.
        text = (
            '[dependencies]\nscalo = { workspace = true, features = ["deployment"] }\n'
        )
        assert dep_features(text, "scalo") == frozenset({"deployment"})

    def test_dev_dependencies_ignored(self) -> None:
        text = (
            "[dev-dependencies]\n"
            'scalo = { version = "2.9", features = ["deployment"] }\n'
        )
        assert dep_features(text, "scalo") is None

    def test_workspace_dependencies_table_read(self) -> None:
        text = (
            "[workspace.dependencies]\n"
            'scalo = { version = "2.9", features = ["deployment"] }\n'
        )
        assert dep_features(text, "scalo") == frozenset({"deployment"})

    def test_comment_stripped_from_entry(self) -> None:
        text = '[dependencies]\nscalo = "2.9"  # the runtime\n'
        assert dep_features(text, "scalo") == frozenset()

    def test_similar_prefix_not_matched(self) -> None:
        text = '[dependencies]\nscalo-extras = { features = ["deployment"] }\n'
        assert dep_features(text, "scalo") is None


class TestPythonEntryPoint:
    """Console-script discovery drives Tier 2 detection."""

    def test_no_pyproject(self, tmp_path: Path) -> None:
        assert python_entry_point(tmp_path) is None

    def test_project_scripts(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n[project.scripts]\ndemo = "demo.main:main"\n',
            encoding="utf-8",
        )
        assert python_entry_point(tmp_path) == "demo"

    def test_no_scripts_table(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["scalo>=2.28"]\n',
            encoding="utf-8",
        )
        assert python_entry_point(tmp_path) is None

    def test_empty_scripts_table(self, tmp_path: Path) -> None:
        # Declared but empty, with a following section — must not claim
        # the next table's first key as a script name.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n'
            "[project.scripts]\n"
            "[tool.ruff]\nline-length = 88\n",
            encoding="utf-8",
        )
        assert python_entry_point(tmp_path) is None

    def test_comments_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project.scripts]\n# the CLI entry\ndemo = "demo.main:main"\n',
            encoding="utf-8",
        )
        assert python_entry_point(tmp_path) == "demo"

    def test_quoted_key_unquoted(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project.scripts]\n"demo-app" = "demo.main:main"\n',
            encoding="utf-8",
        )
        assert python_entry_point(tmp_path) == "demo-app"

    def test_poetry_scripts(self, tmp_path: Path) -> None:
        # A poetry-managed ServiceApp installs a console script just the
        # same, so it is a producer.
        (tmp_path / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "demo"\n'
            '[tool.poetry.scripts]\ndemo = "demo.main:main"\n',
            encoding="utf-8",
        )
        assert python_entry_point(tmp_path) == "demo"

    def test_console_scripts_entry_points(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n'
            '[project.entry-points."console_scripts"]\ndemo = "demo.main:main"\n',
            encoding="utf-8",
        )
        assert python_entry_point(tmp_path) == "demo"


def _write_member(path: Path, name: str, *, binary: bool) -> None:
    """Write a workspace member crate, with or without a binary target."""
    path.mkdir(parents=True)
    body = f'[package]\nname = "{name}"\n'
    body += f'[[bin]]\nname = "{name}"\npath = "src/main.rs"\n' if binary else "[lib]\n"
    (path / "Cargo.toml").write_text(body, encoding="utf-8")
