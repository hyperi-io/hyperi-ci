# Project:   HyperI CI
# File:      tests/unit/test_kube_linter.py
# Purpose:   Tests for the kube-linter k8s best-practice advisory
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.kube_linter - advisory only, never gates."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import kube_linter

_SARIF = json.dumps(
    {
        "runs": [
            {
                "tool": {"driver": {"name": "kube-linter", "rules": []}},
                "results": [
                    {
                        "ruleId": "no-read-only-root-fs",
                        "level": "warning",
                        "message": {"text": "container should run read-only"},
                        "locations": [
                            {"physicalLocation": {"artifactLocation": {"uri": "chart"}}}
                        ],
                    }
                ],
            }
        ]
    }
)


def _cfg(raw: dict | None = None) -> CIConfig:
    return CIConfig(_raw=raw or {})


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    *,
    exe: str | None = "/usr/bin/kube-linter",
) -> None:
    monkeypatch.setattr(kube_linter, "_install_kube_linter", lambda: exe)
    monkeypatch.setattr(kube_linter, "find_tool", lambda *a, **k: exe)
    monkeypatch.setattr(
        kube_linter,
        "run_cmd",
        lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=1),
    )


class TestRun:
    def test_disabled(self) -> None:
        assert (
            kube_linter.run(
                [Path("chart")], _cfg({"quality": {"kube_linter": "disabled"}})
            )
            == 0
        )

    def test_no_targets_skips(self) -> None:
        assert kube_linter.run([], _cfg()) == 0

    def test_findings_never_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub(monkeypatch, _SARIF)
        # returncode 1 from kube-linter (it found violations) must NOT propagate.
        assert kube_linter.run([Path("chart")], _cfg()) == 0

    def test_missing_tool_info_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub(monkeypatch, "", exe=None)
        assert kube_linter.run([Path("chart")], _cfg()) == 0

    def test_oserror_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            kube_linter, "_install_kube_linter", lambda: "/usr/bin/kube-linter"
        )

        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise OSError("exec failed")

        monkeypatch.setattr(kube_linter, "run_cmd", _boom)
        assert kube_linter.run([Path("chart")], _cfg()) == 0
