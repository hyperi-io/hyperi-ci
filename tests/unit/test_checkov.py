# Project:   HyperI CI
# File:      tests/unit/test_checkov.py
# Purpose:   Tests for the Checkov IaC security advisory
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.checkov."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import checkov

_SKIP = "HYPERCI_QUALITY_SKIP"

_SARIF = json.dumps(
    {
        "runs": [
            {
                "tool": {"driver": {"name": "checkov", "rules": []}},
                "results": [
                    {
                        "ruleId": "CKV_K8S_20",
                        "level": "error",
                        "message": {"text": "containers should not run as root"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "chart/deploy.yaml"}
                                }
                            }
                        ],
                    }
                ],
            }
        ]
    }
)


def _cfg(raw: dict | None = None) -> CIConfig:
    return CIConfig(_raw=raw or {})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SKIP, raising=False)
    monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)


def _stub(
    monkeypatch: pytest.MonkeyPatch, sarif_text: str, *, has_tool: bool = True
) -> dict:
    """Stub the checkov invocation; write the sarif the module will read back."""
    captured: dict = {}
    monkeypatch.setattr(checkov, "_base_cmd", lambda: ["checkov"] if has_tool else None)

    def _run(cmd, **kw):  # noqa: ANN001, ANN003
        captured["cmd"] = cmd
        # Emulate checkov writing results_sarif.sarif into --output-file-path.
        out_idx = cmd.index("--output-file-path") + 1
        (Path(cmd[out_idx]) / "results_sarif.sarif").write_text(
            sarif_text, encoding="utf-8"
        )
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(checkov, "run_cmd", _run)
    return captured


class TestRun:
    def test_disabled(self, tmp_path: Path) -> None:
        assert checkov.run(tmp_path, _cfg({"quality": {"checkov": "disabled"}})) == 0

    def test_missing_tool_warn_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, "", has_tool=False)
        assert checkov.run(tmp_path, _cfg()) == 0

    def test_warn_default_never_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, _SARIF)
        assert checkov.run(tmp_path, _cfg()) == 0

    def test_blocking_fails_on_findings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(monkeypatch, _SARIF)
        assert checkov.run(tmp_path, _cfg({"quality": {"checkov": "blocking"}})) == 1

    def test_clean_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub(
            monkeypatch, json.dumps({"runs": [{"tool": {"driver": {}}, "results": []}]})
        )
        assert checkov.run(tmp_path, _cfg({"quality": {"checkov": "blocking"}})) == 0

    def test_default_frameworks_and_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _stub(monkeypatch, _SARIF)
        checkov.run(tmp_path, _cfg())
        cmd = captured["cmd"]
        assert "kubernetes" in cmd and "terraform" in cmd
        assert "--soft-fail" in cmd
        # Default worktree skip-path present.
        assert any(".worktrees" in c for c in cmd)

    def test_config_skip_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _stub(monkeypatch, _SARIF)
        checkov.run(tmp_path, _cfg({"quality": {"checkov": {"skip": ["CKV_K8S_1"]}}}))
        cmd = captured["cmd"]
        assert "--skip-check" in cmd and "CKV_K8S_1" in cmd

    def test_force_skip_disables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_SKIP, "checkov")
        assert checkov.run(tmp_path, _cfg()) == 0

    def test_strict_upgrades_warn_to_blocking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --strict (HYPERCI_QUALITY_STRICT) escalates the default `warn` Checkov
        # to blocking, so a finding now fails.
        monkeypatch.setenv("HYPERCI_QUALITY_STRICT", "1")
        _stub(monkeypatch, _SARIF)
        assert checkov.run(tmp_path, _cfg()) == 1
