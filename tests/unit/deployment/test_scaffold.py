# Project:   HyperI CI
# File:      tests/unit/deployment/test_scaffold.py
# Purpose:   Tests for the init-contract scaffolder
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.deployment.scaffold.init_contract`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperi_ci.deployment.contract import DeploymentContract
from hyperi_ci.deployment.scaffold import (
    EXIT_ALREADY_EXISTS,
    EXIT_INVALID_NAME,
    EXIT_OK,
    init_contract,
)


def _read_contract(directory: Path) -> dict:
    return json.loads(
        (directory / "deployment-contract.json").read_text(encoding="utf-8")
    )


class TestInitContract:
    """Basic happy-path scaffolding."""

    def test_writes_contract_for_valid_name(self, tmp_path: Path) -> None:
        rc = init_contract(tmp_path, "my-app")
        assert rc == EXIT_OK
        assert (tmp_path / "deployment-contract.json").is_file()

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "ci"
        rc = init_contract(target, "my-app")
        assert rc == EXIT_OK
        assert (target / "deployment-contract.json").is_file()

    def test_contract_parses_back_via_pydantic(self, tmp_path: Path) -> None:
        # The whole point of scaffolding — written contract must be
        # immediately consumable by emit-artefacts without editing.
        init_contract(tmp_path, "my-app")
        data = _read_contract(tmp_path)
        contract = DeploymentContract.model_validate(data)
        assert contract.app_name == "my-app"
        assert contract.binary() == "my-app"

    def test_defaults_match_org_conventions(self, tmp_path: Path) -> None:
        init_contract(tmp_path, "my-app")
        data = _read_contract(tmp_path)
        # SCREAMING_SNAKE prefix from hyphen-separated name.
        assert data["env_prefix"] == "MY_APP"
        # snake_case prefix for Prometheus namespace.
        assert data["metric_prefix"] == "my_app"
        # Mount path follows /etc/<app>/<app>.yaml convention.
        assert data["config_mount_path"] == "/etc/my-app/my-app.yaml"

    def test_metrics_port_default(self, tmp_path: Path) -> None:
        init_contract(tmp_path, "my-app")
        data = _read_contract(tmp_path)
        assert data["metrics_port"] == 9090

    def test_health_paths_match_dfe_convention(self, tmp_path: Path) -> None:
        init_contract(tmp_path, "my-app")
        data = _read_contract(tmp_path)
        assert data["health"]["liveness_path"] == "/healthz"
        assert data["health"]["readiness_path"] == "/readyz"
        assert data["health"]["metrics_path"] == "/metrics"

    def test_description_is_empty_for_human_to_fill(self, tmp_path: Path) -> None:
        # Empty so it shows up clearly in PR diffs as a TODO; we don't
        # auto-generate a placeholder string.
        init_contract(tmp_path, "my-app")
        data = _read_contract(tmp_path)
        assert data["description"] == ""

    def test_pretty_printed_json(self, tmp_path: Path) -> None:
        # Indented + trailing newline so the file diffs cleanly when
        # operators edit it later.
        init_contract(tmp_path, "my-app")
        text = (tmp_path / "deployment-contract.json").read_text(encoding="utf-8")
        assert text.startswith("{\n")
        assert text.endswith("}\n")


class TestNameValidation:
    """app_name validation matches the org's repo-naming convention."""

    @pytest.mark.parametrize(
        "name",
        [
            "ci",  # too short (must be 3+ chars)
            "ab",  # too short
            "My-App",  # uppercase
            "my_app",  # underscore
            "my.app",  # dot
            "1app",  # starts with digit
            "-app",  # starts with hyphen
            "app-",  # ends with hyphen
            "",  # empty
        ],
    )
    def test_invalid_names_rejected(self, tmp_path: Path, name: str) -> None:
        rc = init_contract(tmp_path, name)
        assert rc == EXIT_INVALID_NAME
        assert not (tmp_path / "deployment-contract.json").exists()

    @pytest.mark.parametrize(
        "name",
        [
            "abc",
            "my-app",
            "dfe-loader",
            "ci-test-rust-app",
            "long-multi-hyphen-name-with-numbers-1",
        ],
    )
    def test_valid_names_accepted(self, tmp_path: Path, name: str) -> None:
        rc = init_contract(tmp_path, name)
        assert rc == EXIT_OK


class TestForceOverwrite:
    """Without --force, existing contracts are protected."""

    def test_existing_contract_not_overwritten_by_default(self, tmp_path: Path) -> None:
        # First run writes.
        rc = init_contract(tmp_path, "first-app")
        assert rc == EXIT_OK

        # Second run errors instead of clobbering.
        rc = init_contract(tmp_path, "second-app")
        assert rc == EXIT_ALREADY_EXISTS

        # Original content preserved.
        data = _read_contract(tmp_path)
        assert data["app_name"] == "first-app"

    def test_force_overwrites_existing(self, tmp_path: Path) -> None:
        init_contract(tmp_path, "first-app")
        rc = init_contract(tmp_path, "second-app", force=True)
        assert rc == EXIT_OK

        data = _read_contract(tmp_path)
        assert data["app_name"] == "second-app"
