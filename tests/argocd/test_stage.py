# Project:   HyperI CI
# File:      tests/argocd/test_stage.py
# Purpose:   Integration tests for hyperi-ci argocd stage
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Integration tests for the argocd stage.

Mocks subprocess for `<consumer> emit-argocd` and `git` /
`curl` (gitops push). Uses real overlay processing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

from hyperi_ci.argocd.stage import run as argocd_run
from hyperi_ci.config import CIConfig

_BASE_ARGOCD = textwrap.dedent(
    """\
    apiVersion: argoproj.io/v1alpha1
    kind: Application
    metadata:
      name: dfe-loader
    spec:
      source:
        repoURL: oci://ghcr.io/hyperi-io/helm-charts
        chart: dfe-loader
        targetRevision: 1.18.3
      destination:
        server: https://kubernetes.default.svc
        namespace: dfe-dev
    """
)


def _config(
    *,
    enabled: bool = True,
    overlays: list | None = None,
    envs: list | None = None,
) -> CIConfig:
    block: dict = {"enabled": enabled, "binary_name": "test-binary"}
    if overlays is not None:
        block["overlays"] = overlays
    if envs is not None:
        block["envs"] = envs
    return CIConfig(_raw={"publish": {"argocd": block}})


def _mock_emit_argocd(cmd, **kwargs):
    res = MagicMock()
    res.returncode = 0
    res.stdout = _BASE_ARGOCD
    res.stderr = ""
    return res


class TestArgoCDStage:
    def test_disabled_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        cfg = _config(enabled=False)
        with patch(
            "hyperi_ci.argocd.stage.subprocess.run",
            side_effect=AssertionError("should not subprocess"),
        ):
            rc = argocd_run(cfg)
        assert rc == 0

    def test_validate_mode_runs_emit_only(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HYPERCI_PUBLISH_MODE", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
        cfg = _config()
        with patch(
            "hyperi_ci.argocd.stage.subprocess.run",
            side_effect=_mock_emit_argocd,
        ):
            rc = argocd_run(cfg)
        assert rc == 0  # validate succeeded; no push

    def test_overlay_applied_in_validate_mode(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HYPERCI_PUBLISH_MODE", raising=False)
        cfg = _config(
            overlays=[
                {
                    "anchor": "spec.syncPolicy.append",
                    "content": "automated:\n  prune: true\n",
                }
            ]
        )
        with patch(
            "hyperi_ci.argocd.stage.subprocess.run",
            side_effect=_mock_emit_argocd,
        ):
            rc = argocd_run(cfg)
        assert rc == 0

    def test_publish_mode_pushes_to_gitops(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HYPERCI_PUBLISH_MODE", "true")
        monkeypatch.setenv("GITOPS_TOKEN", "fake-token")
        cfg = _config(envs=["dev"])

        # Track gitops_push.push calls instead of mocking subprocess for
        # all the git operations - the gitops_push module has its own
        # tests for those.
        with patch(
            "hyperi_ci.argocd.stage.subprocess.run",
            side_effect=_mock_emit_argocd,
        ):
            with patch(
                "hyperi_ci.argocd.gitops_push.push", return_value=0
            ) as push_mock:
                rc = argocd_run(cfg)
        assert rc == 0
        assert push_mock.call_count == 1
        cfg_arg = push_mock.call_args.args[0]
        assert cfg_arg.repo == "hyperi-io/gitops"
        assert cfg_arg.push_mode == "direct"  # dev env defaults to direct
        assert "applications/" in cfg_arg.path

    def test_prod_env_defaults_to_pr_mode(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HYPERCI_PUBLISH_MODE", "true")
        monkeypatch.setenv("GITOPS_TOKEN", "fake-token")
        cfg = _config(envs=["dev", "prod"])

        with patch(
            "hyperi_ci.argocd.stage.subprocess.run",
            side_effect=_mock_emit_argocd,
        ):
            with patch(
                "hyperi_ci.argocd.gitops_push.push", return_value=0
            ) as push_mock:
                rc = argocd_run(cfg)
        assert rc == 0
        # Two pushes: dev (direct) and prod (pr).
        assert push_mock.call_count == 2
        modes = sorted(call.args[0].push_mode for call in push_mock.call_args_list)
        assert modes == ["direct", "pr"]
