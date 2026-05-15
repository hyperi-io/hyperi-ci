# Project:   HyperI CI
# File:      tests/helm/test_stage.py
# Purpose:   Integration tests for hyperi-ci helm stage
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Integration tests for the helm stage.

Mocks subprocess for `helm` and the consumer's `emit-chart` call.
Uses real fixture chart content + real overlay processing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

from hyperi_ci.config import CIConfig
from hyperi_ci.helm.stage import run as helm_run


def _make_chart(chart_dir: Path) -> None:
    """Write a minimal valid Helm chart into chart_dir."""
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        textwrap.dedent(
            """\
            apiVersion: v2
            name: dfe-transform-vector
            version: 1.0.13
            description: vector wrapper
            """
        ),
        encoding="utf-8",
    )
    (chart_dir / "values.yaml").write_text(
        "replicaCount: 1\n", encoding="utf-8"
    )
    (chart_dir / "templates").mkdir()
    (chart_dir / "templates" / "deployment.yaml").write_text(
        textwrap.dedent(
            """\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: dfe-transform-vector
            spec:
              replicas: {{ .Values.replicaCount }}
            """
        ),
        encoding="utf-8",
    )


def _config(overlays: dict | None = None, *, enabled: bool = True) -> CIConfig:
    """Build a CIConfig with publish.helm settings for the test."""
    helm_block: dict = {"enabled": enabled, "binary_name": "test-binary"}
    if overlays is not None:
        helm_block["overlays"] = overlays
    return CIConfig(_raw={"publish": {"helm": helm_block}})


def _mock_run_factory(emit_chart_dir: Path | None):
    """Build a subprocess.run mock that:
       - emit-chart: writes a chart into the path arg
       - helm lint: success
       - helm template: returns a minimal rendered yaml
       - helm package: returns a path to a fake .tgz
       - helm push: success
    """
    package_path: dict = {"path": None}

    def _run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""

        # emit-chart subcommand from the consumer binary
        if (
            len(cmd) >= 3
            and cmd[1] == "emit-chart"
            and emit_chart_dir is None
        ):
            chart_target = Path(cmd[2])
            _make_chart(chart_target)
            return result

        # helm lint
        if cmd[:2] == ["helm", "lint"]:
            return result

        # helm template
        if cmd[:2] == ["helm", "template"]:
            result.stdout = textwrap.dedent(
                """\
                apiVersion: apps/v1
                kind: Deployment
                metadata:
                  name: dfe-transform-vector
                spec:
                  replicas: 1
                """
            )
            return result

        # helm package
        if cmd[:2] == ["helm", "package"]:
            dest = Path(cmd[-1])
            tgz = dest / "dfe-transform-vector-1.0.13.tgz"
            tgz.write_bytes(b"fake-tgz")
            package_path["path"] = tgz
            result.stdout = (
                f"Successfully packaged chart and saved it to: {tgz}"
            )
            return result

        # helm push
        if cmd[:2] == ["helm", "push"]:
            return result

        # Unknown subprocess call — fail loudly so the test catches drift.
        result.returncode = 99
        result.stderr = f"unmocked subprocess call: {cmd!r}"
        return result

    return _run


class TestHelmStage:
    def test_disabled_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        cfg = _config(enabled=False)
        # No subprocess calls expected — failing if any go through the
        # mock would surface unintended side effects.
        with patch(
            "hyperi_ci.helm.stage.subprocess.run",
            side_effect=AssertionError("should not subprocess"),
        ):
            rc = helm_run(cfg)
        assert rc == 0

    def test_no_helm_binary_fails_clean(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        cfg = _config()
        with patch("hyperi_ci.helm.stage.shutil.which", return_value=None):
            rc = helm_run(cfg)
        assert rc == 1

    def test_validate_mode_runs_pipeline_without_push(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # No publish-mode env vars → validate mode by default
        monkeypatch.delenv("HYPERCI_PUBLISH_MODE", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

        cfg = _config()
        run_mock = _mock_run_factory(emit_chart_dir=None)
        with patch("hyperi_ci.helm.stage.shutil.which", return_value="/usr/bin/helm"):
            with patch(
                "hyperi_ci.helm.stage.subprocess.run", side_effect=run_mock
            ):
                rc = helm_run(cfg)
        assert rc == 0  # validate succeeds without push

    def test_publish_mode_pushes_to_oci(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HYPERCI_PUBLISH_MODE", "true")

        cfg = _config()
        recorded_calls: list = []

        run_mock = _mock_run_factory(emit_chart_dir=None)

        def _wrapper(cmd, **kwargs):
            recorded_calls.append(cmd)
            return run_mock(cmd, **kwargs)

        with patch("hyperi_ci.helm.stage.shutil.which", return_value="/usr/bin/helm"):
            with patch(
                "hyperi_ci.helm.stage.subprocess.run", side_effect=_wrapper
            ):
                rc = helm_run(cfg)
        assert rc == 0
        # `helm push` must have been called with the right registry.
        push_calls = [
            c for c in recorded_calls if c[:2] == ["helm", "push"]
        ]
        assert len(push_calls) == 1
        assert push_calls[0][-1] == "oci://ghcr.io/hyperi-io/helm-charts"

    def test_overlay_adds_inserted_into_chart(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HYPERCI_PUBLISH_MODE", raising=False)

        cfg = _config(
            overlays={
                "adds": [
                    {
                        "path": "templates/extra-pvc.yaml",
                        "content": "kind: PersistentVolumeClaim\n",
                    }
                ]
            }
        )
        run_mock = _mock_run_factory(emit_chart_dir=None)
        captured: dict = {"path": None, "content": None}

        def _capturing_run(cmd, **kwargs):
            # Snapshot the file CONTENT during helm-lint (before the
            # tempdir cleanup at end of helm_run wipes it).
            res = run_mock(cmd, **kwargs)
            if cmd[:2] == ["helm", "lint"]:
                chart_dir = Path(cmd[-1])
                pvc = chart_dir / "templates" / "extra-pvc.yaml"
                if pvc.exists():
                    captured["path"] = str(pvc.relative_to(chart_dir))
                    captured["content"] = pvc.read_text(encoding="utf-8")
            return res

        with patch("hyperi_ci.helm.stage.shutil.which", return_value="/usr/bin/helm"):
            with patch(
                "hyperi_ci.helm.stage.subprocess.run",
                side_effect=_capturing_run,
            ):
                rc = helm_run(cfg)
        assert rc == 0
        assert captured["path"] == "templates/extra-pvc.yaml", (
            f"overlay add should have written templates/extra-pvc.yaml, "
            f"got: {captured}"
        )
        assert captured["content"] == "kind: PersistentVolumeClaim\n"
