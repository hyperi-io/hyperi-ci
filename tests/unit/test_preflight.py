# Project:   HyperI CI
# File:      tests/unit/test_preflight.py
# Purpose:   Credentials are checked before the build, and only the real ones
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Preflight must catch a missing token without blocking a release that needs none.

A false block is worse than the problem it solves: it stops a shipping release
on a credential the publish stage would never have used. So the cases that must
NOT block are asserted as carefully as the ones that must.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.preflight import check_publish_credentials, run_preflight

_ALL_TOKENS = (
    "CARGO_REGISTRY_TOKEN",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


@pytest.fixture(autouse=True)
def no_ambient_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's real tokens must not decide the result."""
    for name in _ALL_TOKENS:
        monkeypatch.delenv(name, raising=False)


def _config(**destinations: object) -> CIConfig:
    return CIConfig(_raw={"publish": {"destinations_oss": destinations}})


def _rust_lib(tmp_path: Path) -> Path:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "thing"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("", encoding="utf-8")
    return tmp_path


class TestBlocking:
    """Only where the publish handler hard-fails without the credential."""

    def test_missing_cargo_token_blocks_a_library_crate(self, tmp_path: Path) -> None:
        with patch("hyperi_ci.preflight._publishes_a_crate", return_value=True):
            rc = check_publish_credentials(
                _config(cargo="crates-io"), project_dir=_rust_lib(tmp_path)
            )
        assert rc == 1

    def test_present_cargo_token_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARGO_REGISTRY_TOKEN", "tok")
        with patch("hyperi_ci.preflight._publishes_a_crate", return_value=True):
            rc = check_publish_credentials(
                _config(cargo="crates-io"), project_dir=_rust_lib(tmp_path)
            )
        assert rc == 0

    def test_missing_npm_token_blocks(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name": "t"}', encoding="utf-8")
        (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
        assert (
            check_publish_credentials(_config(npm="npmjs"), project_dir=tmp_path) == 1
        )


class TestMustNotBlock:
    """The false-block cases, which are the expensive kind of wrong."""

    def test_a_rust_binary_app_needs_no_crates_token(self, tmp_path: Path) -> None:
        """rust.publish.run returns early for a crate with [[bin]] targets."""
        with patch("hyperi_ci.preflight._publishes_a_crate", return_value=False):
            rc = check_publish_credentials(
                _config(cargo="crates-io", binaries="r2-binaries"),
                project_dir=_rust_lib(tmp_path),
            )
        assert rc == 0

    def test_missing_pypi_token_warns_only(self, tmp_path: Path) -> None:
        """The PyPI upload falls back to OIDC trusted publishing."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "t"\nversion = "1.0.0"\n', encoding="utf-8"
        )
        assert (
            check_publish_credentials(_config(python="pypi"), project_dir=tmp_path) == 0
        )

    def test_missing_r2_keys_warn_only(self, tmp_path: Path) -> None:
        """Binaries still reach GitHub Releases; R2 is one destination of two."""
        assert (
            check_publish_credentials(
                _config(binaries="r2-binaries"), project_dir=tmp_path
            )
            == 0
        )

    def test_an_opted_out_destination_is_not_checked(self, tmp_path: Path) -> None:
        with patch("hyperi_ci.preflight._publishes_a_crate", return_value=True):
            rc = check_publish_credentials(
                _config(cargo=False), project_dir=_rust_lib(tmp_path)
            )
        assert rc == 0

    def test_no_destinations_configured(self, tmp_path: Path) -> None:
        assert check_publish_credentials(_config(), project_dir=tmp_path) == 0

    def test_a_go_project_needs_nothing(self, tmp_path: Path) -> None:
        """proxy.golang.org takes no credential."""
        (tmp_path / "go.mod").write_text("module example.com/t\n", encoding="utf-8")
        assert (
            check_publish_credentials(_config(go="go-proxy"), project_dir=tmp_path) == 0
        )


class TestRunPreflight:
    def test_no_op_outside_ci(self, tmp_path: Path) -> None:
        """Credentials are a CI concern; a local run must not fail on them."""
        with patch("hyperi_ci.preflight.is_ci", return_value=False):
            with patch("hyperi_ci.preflight._publishes_a_crate", return_value=True):
                rc = run_preflight(
                    _config(cargo="crates-io"), project_dir=_rust_lib(tmp_path)
                )
        assert rc == 0

    def test_checks_in_ci(self, tmp_path: Path) -> None:
        with patch("hyperi_ci.preflight.is_ci", return_value=True):
            with patch("hyperi_ci.preflight._publishes_a_crate", return_value=True):
                rc = run_preflight(
                    _config(cargo="crates-io"), project_dir=_rust_lib(tmp_path)
                )
        assert rc == 1
