# Project:   HyperI CI
# File:      tests/unit/test_lint_manifests.py
# Purpose:   Tests for the lint-manifests orchestrator (Path B)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.lint_manifests - the k8s/IaC orchestrator.

kubeconform gates always; Checkov gates only when escalated to blocking;
kube-linter is always advisory. All tools run even after a gate fails. Sub-tools
are mocked here - this pins the orchestration, not the tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import lint_manifests


def _cfg() -> CIConfig:
    return CIConfig(_raw={})


@pytest.fixture
def _wire(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Mock discovery + the three tools; record which ran."""
    calls: dict = {"kubeconform": 0, "kube_linter": 0, "checkov": 0, "rendered": None}

    monkeypatch.setattr(
        lint_manifests, "discover_helm_charts", lambda *a, **k: [Path("charts/svc")]
    )
    monkeypatch.setattr(
        lint_manifests, "discover_manifests", lambda *a, **k: [Path("argocd/app.yaml")]
    )
    monkeypatch.setattr(lint_manifests.render, "helm_available", lambda: True)
    monkeypatch.setattr(
        lint_manifests.render, "render_charts", lambda charts, out: [Path("r.yaml")]
    )

    def _kc(targets, config, **k):  # noqa: ANN001, ANN003
        calls["kubeconform"] += 1
        calls["kc_targets"] = targets
        return calls.get("kc_rc", 0)

    def _kl(targets, config, **k):  # noqa: ANN001, ANN003
        calls["kube_linter"] += 1
        return 0

    def _cv(root, config, **k):  # noqa: ANN001, ANN003
        calls["checkov"] += 1
        return calls.get("cv_rc", 0)

    monkeypatch.setattr(lint_manifests.kubeconform, "run", _kc)
    monkeypatch.setattr(lint_manifests.kube_linter, "run", _kl)
    monkeypatch.setattr(lint_manifests.checkov, "run", _cv)
    return calls


class TestRun:
    def test_gate_pass_returns_zero(self, _wire: dict, tmp_path: Path) -> None:
        assert lint_manifests.run(tmp_path, _cfg()) == 0

    def test_gate_failure_propagates(self, _wire: dict, tmp_path: Path) -> None:
        _wire["kc_rc"] = 1
        assert lint_manifests.run(tmp_path, _cfg()) == 1

    def test_all_tools_run(self, _wire: dict, tmp_path: Path) -> None:
        lint_manifests.run(tmp_path, _cfg())
        assert _wire["kubeconform"] == 1
        assert _wire["kube_linter"] == 1
        assert _wire["checkov"] == 1

    def test_advisories_run_even_when_gate_fails(
        self, _wire: dict, tmp_path: Path
    ) -> None:
        _wire["kc_rc"] = 1
        lint_manifests.run(tmp_path, _cfg())
        assert _wire["kube_linter"] == 1 and _wire["checkov"] == 1

    def test_blocking_checkov_fails_verb_even_when_gate_passes(
        self, _wire: dict, tmp_path: Path
    ) -> None:
        # kubeconform passes (0) but a blocking Checkov returns 1 -> verb fails.
        _wire["kc_rc"] = 0
        _wire["cv_rc"] = 1
        assert lint_manifests.run(tmp_path, _cfg()) == 1

    def test_kubeconform_gets_rendered_plus_plain(
        self, _wire: dict, tmp_path: Path
    ) -> None:
        lint_manifests.run(tmp_path, _cfg())
        # rendered chart (r.yaml) + plain manifest (argocd/app.yaml)
        assert Path("r.yaml") in _wire["kc_targets"]
        assert Path("argocd/app.yaml") in _wire["kc_targets"]

    def test_helm_unavailable_still_runs_advisories(
        self, _wire: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lint_manifests.render, "helm_available", lambda: False)
        monkeypatch.setattr(lint_manifests, "is_ci", lambda: False)
        lint_manifests.run(tmp_path, _cfg())
        # No rendered charts, but the plain manifest is still validated.
        assert _wire["kc_targets"] == [Path("argocd/app.yaml")]
        assert _wire["kube_linter"] == 1

    def test_helm_absent_blocking_gate_fails_in_ci(
        self, _wire: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A chart exists, helm is absent, kubeconform is blocking (default) and
        # we are in CI -> the gate must NOT pass green having skipped the chart.
        monkeypatch.setattr(lint_manifests.render, "helm_available", lambda: False)
        monkeypatch.setattr(lint_manifests, "is_ci", lambda: True)
        _wire["kc_rc"] = 0
        assert lint_manifests.run(tmp_path, _cfg()) == 1

    def test_helm_absent_locally_does_not_force_fail(
        self, _wire: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lint_manifests.render, "helm_available", lambda: False)
        monkeypatch.setattr(lint_manifests, "is_ci", lambda: False)
        _wire["kc_rc"] = 0
        assert lint_manifests.run(tmp_path, _cfg()) == 0

    def test_render_failure_fails_blocking_gate_in_ci(
        self, _wire: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # helm IS available but the chart fails to render (render_charts returns
        # fewer than discovered) -> the chart was not schema-validated, so a
        # blocking gate must fail rather than pass green (No silent skips).
        monkeypatch.setattr(
            lint_manifests.render, "render_charts", lambda charts, out: []
        )
        monkeypatch.setattr(lint_manifests, "is_ci", lambda: True)
        _wire["kc_rc"] = 0
        assert lint_manifests.run(tmp_path, _cfg()) == 1

    def test_no_targets_blocking_checkov_gates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pure-IaC repo, no charts/manifests, blocking Checkov finds something
        # -> the verb fails (consistent with the has-manifests path).
        monkeypatch.setattr(lint_manifests, "discover_helm_charts", lambda *a, **k: [])
        monkeypatch.setattr(lint_manifests, "discover_manifests", lambda *a, **k: [])
        monkeypatch.setattr(lint_manifests.checkov, "run", lambda *a, **k: 1)
        assert lint_manifests.run(tmp_path, _cfg()) == 1

    def test_no_targets_runs_checkov_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lint_manifests, "discover_helm_charts", lambda *a, **k: [])
        monkeypatch.setattr(lint_manifests, "discover_manifests", lambda *a, **k: [])
        ran = {"checkov": 0, "kubeconform": 0}
        monkeypatch.setattr(
            lint_manifests.checkov,
            "run",
            lambda *a, **k: ran.__setitem__("checkov", 1) or 0,
        )
        monkeypatch.setattr(
            lint_manifests.kubeconform,
            "run",
            lambda *a, **k: ran.__setitem__("kubeconform", 1) or 0,
        )
        assert lint_manifests.run(tmp_path, _cfg()) == 0
        assert ran["checkov"] == 1
        assert ran["kubeconform"] == 0  # nothing to schema-validate
