# Project:   HyperI CI
# File:      tests/unit/deployment/test_emit_artefacts_cli.py
# Purpose:   emit-artefacts CLI subcommand tests
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.deployment.cli.emit_artefacts`.

Covers the parts of the CLI flow that DON'T depend on Phase 2 generators:
arg resolution, contract loading, Pydantic validation, schema_version
gate, exit codes. The actual file-write step currently returns
EXIT_NOT_IMPLEMENTED (=5); that test checks we error with a clear
message until the generators land.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyperi_ci.deployment.cli import (
    EXIT_CONTRACT_INVALID,
    EXIT_CONTRACT_MISSING,
    EXIT_NOT_IMPLEMENTED,
    emit_artefacts,
)


def _valid_contract_dict() -> dict:
    """Return a minimal valid contract dict for tests."""
    return {
        "app_name": "ci-test-app",
        "metrics_port": 9090,
        "health": {
            "liveness_path": "/healthz",
            "readiness_path": "/readyz",
            "metrics_path": "/metrics",
        },
        "env_prefix": "CI_TEST_APP",
        "metric_prefix": "ci_test",
        "config_mount_path": "/etc/ci-test/config.yaml",
    }


def _write_contract(directory: Path, data: dict) -> Path:
    ci_dir = directory / "ci"
    ci_dir.mkdir(parents=True, exist_ok=True)
    path = ci_dir / "deployment-contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestArgResolution:
    """The CLI resolves the contract path from output_dir + --from intelligently."""

    def test_explicit_from_path(self, tmp_path: Path) -> None:
        contract_path = tmp_path / "custom.json"
        contract_path.write_text(json.dumps(_valid_contract_dict()), "utf-8")
        rc = emit_artefacts(tmp_path / "ci", contract_path)
        # Phase 2 generators not implemented yet → EXIT_NOT_IMPLEMENTED.
        # The test passes if we got past contract loading + validation,
        # which is the point of this case.
        assert rc == EXIT_NOT_IMPLEMENTED

    def test_default_path_relative_to_output_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # When output_dir is "ci/" and contains the contract, that's the
        # source. Supports the in-place regen idiom.
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "deployment-contract.json").write_text(
            json.dumps(_valid_contract_dict()), "utf-8"
        )
        monkeypatch.chdir(tmp_path)
        rc = emit_artefacts(ci_dir, None)
        assert rc == EXIT_NOT_IMPLEMENTED

    def test_default_path_falls_back_to_cwd(self, tmp_path: Path, monkeypatch) -> None:
        # When output_dir isn't named ci/, look for ci/deployment-contract.json
        # under the cwd.
        _write_contract(tmp_path, _valid_contract_dict())
        monkeypatch.chdir(tmp_path)
        rc = emit_artefacts(tmp_path / "ci-tmp", None)
        assert rc == EXIT_NOT_IMPLEMENTED


class TestContractMissing:
    """Missing contracts produce a clear error and the right exit code."""

    def test_missing_explicit_path(self, tmp_path: Path) -> None:
        rc = emit_artefacts(tmp_path / "ci-tmp", tmp_path / "does-not-exist.json")
        assert rc == EXIT_CONTRACT_MISSING

    def test_missing_default_path(self, tmp_path: Path, monkeypatch) -> None:
        # No ci/deployment-contract.json in cwd → missing.
        monkeypatch.chdir(tmp_path)
        rc = emit_artefacts(tmp_path / "ci-tmp", None)
        assert rc == EXIT_CONTRACT_MISSING


class TestContractInvalid:
    """Invalid JSON or invalid schema fails before any file writes."""

    def test_malformed_json(self, tmp_path: Path) -> None:
        contract = tmp_path / "bad.json"
        contract.write_text("{ not valid json", encoding="utf-8")
        rc = emit_artefacts(tmp_path / "ci-tmp", contract)
        assert rc == EXIT_CONTRACT_INVALID

    def test_root_not_object(self, tmp_path: Path) -> None:
        contract = tmp_path / "list.json"
        contract.write_text(json.dumps(["not", "an", "object"]), "utf-8")
        rc = emit_artefacts(tmp_path / "ci-tmp", contract)
        assert rc == EXIT_CONTRACT_INVALID

    def test_missing_required_field(self, tmp_path: Path) -> None:
        d = _valid_contract_dict()
        del d["app_name"]
        contract = tmp_path / "bad.json"
        contract.write_text(json.dumps(d), "utf-8")
        rc = emit_artefacts(tmp_path / "ci-tmp", contract)
        assert rc == EXIT_CONTRACT_INVALID

    def test_unknown_field(self, tmp_path: Path) -> None:
        # extra=forbid catches schema drift early.
        d = _valid_contract_dict()
        d["unknown_extra_field"] = "boom"
        contract = tmp_path / "extra.json"
        contract.write_text(json.dumps(d), "utf-8")
        rc = emit_artefacts(tmp_path / "ci-tmp", contract)
        assert rc == EXIT_CONTRACT_INVALID


class TestSchemaVersionGate:
    """schema_version > MAX_SUPPORTED is rejected as invalid contract."""

    def test_too_new_schema(self, tmp_path: Path) -> None:
        d = _valid_contract_dict()
        d["schema_version"] = 99
        contract = tmp_path / "future.json"
        contract.write_text(json.dumps(d), "utf-8")
        rc = emit_artefacts(tmp_path / "ci-tmp", contract)
        # Falls into the same category as invalid contract because the
        # validator runs as part of model parsing.
        assert rc == EXIT_CONTRACT_INVALID

    def test_zero_schema_rejected(self, tmp_path: Path) -> None:
        d = _valid_contract_dict()
        d["schema_version"] = 0
        contract = tmp_path / "zero.json"
        contract.write_text(json.dumps(d), "utf-8")
        rc = emit_artefacts(tmp_path / "ci-tmp", contract)
        assert rc == EXIT_CONTRACT_INVALID


class TestNotImplemented:
    """Phase 2 isn't done; valid contract still returns EXIT_NOT_IMPLEMENTED.

    The user-facing log output (which advertises the would-be artefact list)
    goes through hyperi_pylib's loguru sink and bypasses pytest's stdout/
    stderr capture, so we only assert on the exit-code contract here.
    The advertised file list is covered separately via ARTEFACT_FILES
    being importable from the cli module.
    """

    def test_valid_contract_returns_not_implemented(self, tmp_path: Path) -> None:
        contract = tmp_path / "ok.json"
        contract.write_text(json.dumps(_valid_contract_dict()), "utf-8")
        rc = emit_artefacts(tmp_path / "ci-tmp", contract)
        assert rc == EXIT_NOT_IMPLEMENTED

    def test_artefact_file_list_is_complete(self) -> None:
        """The advertised artefact list covers everything the spec calls for."""
        from hyperi_ci.deployment.cli import ARTEFACT_FILES

        # Spec section "ci/ directory contents" — these are the files
        # every emit-artefacts run produces. Mirroring the order
        # documented in the spec.
        expected = {
            "Dockerfile",
            "Dockerfile.runtime",
            "container-manifest.json",
            "argocd-application.yaml",
            "chart/",
            "deployment-contract.schema.json",
        }
        assert set(ARTEFACT_FILES) == expected
