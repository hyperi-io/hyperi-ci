# Project:   HyperI CI
# File:      tests/unit/test_cli.py
# Purpose:   Tests for CLI argument parsing
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

# Disable auto-update in subprocess-based CLI tests to prevent real PyPI queries
_TEST_ENV = {**os.environ, "HYPERCI_AUTO_UPDATE": "false"}


class TestCLI:
    """CLI entry point tests."""

    def test_version_flag(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "hyperi_ci.cli", "--version"],
            capture_output=True,
            text=True,
            env=_TEST_ENV,
        )
        assert result.returncode == 0
        assert "hyperi-ci" in result.stdout


class TestSourceCheckoutProvenance:
    """`--version` says when the number came from a checkout, not a release.

    A project's `.venv/bin` shim precedes the installed tool on PATH, and the
    committed VERSION is stale in every hyperi-ci repo, so a bare number is
    routinely read as the released one.
    """

    def _distribution(self, payload: str | None):
        class _Dist:
            def read_text(self, _name: str) -> str | None:
                return payload

        return lambda _name: _Dist()

    def test_editable_install_reports_its_path(self) -> None:
        from hyperi_ci import cli

        raw = '{"url":"file:///repo","dir_info":{"editable":true}}'
        with patch.object(cli, "distribution", self._distribution(raw)):
            assert cli._source_checkout() == "/repo"

    def test_non_editable_direct_install_is_not_flagged(self) -> None:
        from hyperi_ci import cli

        raw = '{"url":"file:///repo","dir_info":{}}'
        with patch.object(cli, "distribution", self._distribution(raw)):
            assert cli._source_checkout() is None

    def test_index_install_has_no_direct_url(self) -> None:
        from hyperi_ci import cli

        with patch.object(cli, "distribution", self._distribution(None)):
            assert cli._source_checkout() is None

    def test_malformed_metadata_is_not_fatal(self) -> None:
        from hyperi_ci import cli

        with patch.object(cli, "distribution", self._distribution("{not json")):
            assert cli._source_checkout() is None

    def test_a_missing_distribution_is_not_fatal(self) -> None:
        from hyperi_ci import cli

        def boom(_name: str):
            raise ValueError("no such distribution")

        with patch.object(cli, "distribution", boom):
            assert cli._source_checkout() is None

    def test_checkout_version_resolves_the_tree_not_frozen_metadata(
        self, tmp_path
    ) -> None:
        """The whole point: an editable install's baked number is ignored."""
        from hyperi_ci import cli

        (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        assert cli._checkout_version(str(tmp_path)) == "9.9.9"

    def test_checkout_version_falls_back_when_the_checkout_is_gone(self) -> None:
        """A stale direct_url.json must not make `--version` the failure."""
        from hyperi_ci import cli

        def boom(_root):
            raise OSError("checkout deleted")

        with patch.object(cli, "build_version", boom):
            assert cli._checkout_version("/gone") == cli.__version__

    def test_detect_in_empty_dir(self, tmp_path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyperi_ci.cli",
                "detect",
                "--project-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            env=_TEST_ENV,
        )
        assert result.returncode == 1

    def test_detect_python_project(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyperi_ci.cli",
                "detect",
                "--project-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            env=_TEST_ENV,
        )
        assert result.returncode == 0
        assert "python" in result.stdout

    def test_invalid_stage_fails(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "hyperi_ci.cli", "run", "invalid"],
            capture_output=True,
            text=True,
            env=_TEST_ENV,
        )
        assert result.returncode != 0

    def test_config_defaults_to_yaml(self, tmp_path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("language: rust\n")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyperi_ci.cli",
                "config",
                "--project-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            env=_TEST_ENV,
        )
        assert result.returncode == 0
        assert "rust" in result.stdout
        # YAML output has unquoted keys with colon
        assert "language: rust" in result.stdout
        # JSON would have quoted keys — YAML does not
        assert '"language"' not in result.stdout

    def test_config_json_flag(self, tmp_path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("language: rust\n")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyperi_ci.cli",
                "config",
                "--project-dir",
                str(tmp_path),
                "--json",
            ],
            capture_output=True,
            text=True,
            env=_TEST_ENV,
        )
        assert result.returncode == 0
        assert "rust" in result.stdout
        # JSON output has quoted keys and braces
        assert '"language"' in result.stdout
        assert "{" in result.stdout


class TestRunnerImageBake:
    """The commands a runner image Dockerfile calls to pre-bake tooling.

    A pre-baked tool only pays off if it is exactly what hyperi-ci would have
    installed anyway -- otherwise it is either skipped as already-present
    (silently overriding the pinned version) or reinstalled over the top
    (wasting the image build). These cover the fan-out that makes baking BY
    hyperi-ci possible.
    """

    def test_install_all_dry_run_covers_both_categories(self, tmp_path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "hyperi_ci.cli", "install-all", "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env=_TEST_ENV,
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        # Every toolchain family AND every native-deps language, because an
        # image is built with no project in front of it.
        for expected in (
            "toolchains/llvm",
            "toolchains/gcc",
            "native-deps/rust",
            "native-deps/golang",
            "native-deps/python",
            "native-deps/typescript",
        ):
            assert expected in combined, f"install-all skipped {expected}"

    def test_install_all_excludes_bake_false_entries(self, tmp_path) -> None:
        """`bake: false` stays install-on-demand even in a full bake.

        Non-coinstallable toolsets declare Conflicts, so baking one version
        would lock out jobs needing another.
        """
        result = subprocess.run(
            [sys.executable, "-m", "hyperi_ci.cli", "install-all", "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env=_TEST_ENV,
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "llvm-non-coinstallable: skip" in combined

    def test_install_native_deps_defaults_to_every_language(self, tmp_path) -> None:
        """Bare `install-native-deps --all` fans out, matching install-toolchains."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyperi_ci.cli",
                "install-native-deps",
                "--all",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env=_TEST_ENV,
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        # rust.yaml and typescript.yaml both carry entries -- seeing both
        # proves the fan-out rather than a single default language.
        assert "mold linker" in combined
        assert "sharp / image processing" in combined
