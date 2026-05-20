# Project:   HyperI CI
# File:      tests/helm/test_stage_topology_mode.py
# Purpose:   Tests for helm stage topology-mode (stitcher integration)
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for helm stage's topology-mode integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.helm import stage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOPOLOGY_YAML = """\
apiVersion: hyperi.io/v1
kind: DeploymentTopology
metadata:
  name: default
spec:
  umbrella:
    name: hyperi-deployment-default
    description: Default HyperI deployment topology
    appVersion: "1.0"
  apps:
    - name: dfe-loader
      version: "^1.18"
"""

_TOPOLOGY_YAML_WITH_THIRD_PARTY = """\
apiVersion: hyperi.io/v1
kind: DeploymentTopology
metadata:
  name: full
spec:
  umbrella:
    name: hyperi-deployment-full
    description: Full topology with third-party charts
    appVersion: "1.0"
  apps:
    - name: dfe-loader
      version: "^1.18"
  thirdParty:
    - name: strimzi-kafka-operator
      repository: oci://quay.io/strimzi-helm
      version: "^0.40"
"""


def _make_topo_dir(
    base: Path, name: str = "default", content: str = _TOPOLOGY_YAML
) -> Path:
    """Create a topology directory with a topology.yaml inside."""
    topo_dir = base / "topologies" / name
    topo_dir.mkdir(parents=True)
    (topo_dir / "topology.yaml").write_text(content, encoding="utf-8")
    return topo_dir


def _cfg(topo_path: str | Path | None, *, topology_mode: bool = True) -> CIConfig:
    """Build a CIConfig with publish.helm topology settings."""
    helm_block: dict = {"enabled": True, "topology_mode": topology_mode}
    if topo_path is not None:
        helm_block["topology"] = str(topo_path)
    return CIConfig(_raw={"publish": {"helm": helm_block}})


def _mock_subprocess_ok(*args, **kwargs) -> MagicMock:
    """Generic subprocess.run mock that succeeds."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = ""
    m.stderr = ""
    return m


def _mock_package_subprocess(dest_dir_holder: list) -> object:
    """subprocess.run mock that writes a fake .tgz when helm package is called."""

    def _run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        if list(cmd[:2]) == ["helm", "package"]:
            # cmd: ["helm", "package", <chart_dir>, "-d", <dest>]
            dest = Path(cmd[-1])
            dest_dir_holder.append(dest)
            tgz = dest / "hyperi-deployment-default-1.0.tgz"
            tgz.write_bytes(b"fake-tgz")
            m.stdout = f"Successfully packaged chart and saved it to: {tgz}"
        return m

    return _run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTopologyModeDetection:
    """topology_mode dispatch is triggered; normal emit-chart path is skipped."""

    def test_topology_mode_flag_routes_to_stitcher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """topology_mode: true causes stitcher path, not emit-chart subprocess."""
        topo_dir = _make_topo_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        cfg = _cfg(topo_dir)

        # Patch OCI resolution to avoid real network call
        from hyperi_ci.deployment.topology import resolve as resolve_mod

        monkeypatch.setattr(
            resolve_mod, "_fetch_available", lambda r, c: {n: ["1.18.3"] for n in c}
        )

        dest_holder: list = []
        pkg_mock = _mock_package_subprocess(dest_holder)

        with (
            patch(
                "hyperi_ci.deployment.topology.stitch.shutil.which",
                return_value="/usr/bin/helm",
            ),
            patch(
                "hyperi_ci.deployment.topology.stitch.subprocess.run",
                side_effect=_mock_subprocess_ok,
            ),
            patch("hyperi_ci.helm.stage.subprocess.run", side_effect=pkg_mock),
        ):
            rc = stage.run(cfg)

        assert rc == 0, f"Expected 0 (validate mode), got {rc}"

    def test_topology_mode_false_does_not_dispatch_to_stitcher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """topology_mode: false falls through to the standard emit-chart path."""
        monkeypatch.chdir(tmp_path)
        cfg = _cfg(topo_path=None, topology_mode=False)
        cfg = CIConfig(
            _raw={
                "publish": {
                    "helm": {
                        "enabled": True,
                        "topology_mode": False,
                        "binary_name": "test-binary",
                    }
                }
            }
        )

        # Standard path hits shutil.which for helm; return None to trigger the
        # "helm not on PATH" guard (rc=1) without running emit-chart.
        with patch("hyperi_ci.helm.stage.shutil.which", return_value=None):
            rc = stage.run(cfg)

        assert rc == 1  # helm not found — standard path gate, not topology path


class TestTopologyModeValidation:
    """Misconfiguration exits are tested here (exit code 1 cases)."""

    def test_missing_topology_key_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """publish.helm.topology not set → exit 1."""
        monkeypatch.chdir(tmp_path)
        cfg = _cfg(topo_path=None)

        rc = stage.run(cfg)
        assert rc == 1

    def test_topology_dir_missing_topology_yaml_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """topology path set but no topology.yaml → exit 1."""
        empty_dir = tmp_path / "topologies" / "empty"
        empty_dir.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        cfg = _cfg(empty_dir)
        rc = stage.run(cfg)
        assert rc == 1


class TestTopologyModeVersionResolution:
    """Version resolution errors produce exit code 3."""

    def test_resolution_failure_returns_exit_3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When OCI resolution fails for a chart, exit 3 is returned."""
        topo_dir = _make_topo_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        cfg = _cfg(topo_dir)

        from hyperi_ci.deployment.topology import resolve as resolve_mod

        # Return empty version list → VersionResolutionError inside resolver
        monkeypatch.setattr(
            resolve_mod, "_fetch_available", lambda r, c: {n: [] for n in c}
        )

        rc = stage.run(cfg)
        assert rc == 3


class TestTopologyModeStitchAndPackage:
    """Successful stitch → validate (no push) returns 0."""

    def test_validate_mode_succeeds_without_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validate mode (no HYPERCI_PUBLISH_MODE): stitch + package, no push."""
        topo_dir = _make_topo_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HYPERCI_PUBLISH_MODE", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

        cfg = _cfg(topo_dir)

        from hyperi_ci.deployment.topology import resolve as resolve_mod

        monkeypatch.setattr(
            resolve_mod, "_fetch_available", lambda r, c: {n: ["1.18.3"] for n in c}
        )

        dest_holder: list = []
        pkg_mock = _mock_package_subprocess(dest_holder)

        with (
            patch(
                "hyperi_ci.deployment.topology.stitch.shutil.which",
                return_value="/usr/bin/helm",
            ),
            patch(
                "hyperi_ci.deployment.topology.stitch.subprocess.run",
                side_effect=_mock_subprocess_ok,
            ),
            patch("hyperi_ci.helm.stage.subprocess.run", side_effect=pkg_mock),
        ):
            rc = stage.run(cfg)

        assert rc == 0

    def test_publish_mode_calls_helm_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Publish mode: stitch + package + push → exit 0."""
        topo_dir = _make_topo_dir(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HYPERCI_PUBLISH_MODE", "true")

        cfg = _cfg(topo_dir)

        from hyperi_ci.deployment.topology import resolve as resolve_mod

        monkeypatch.setattr(
            resolve_mod, "_fetch_available", lambda r, c: {n: ["1.18.3"] for n in c}
        )

        recorded_stage_calls: list = []

        def _stage_subprocess(cmd, **kwargs):
            recorded_stage_calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            if list(cmd[:2]) == ["helm", "package"]:
                dest = Path(cmd[-1])
                tgz = dest / "hyperi-deployment-default-1.0.tgz"
                tgz.write_bytes(b"fake")
                m.stdout = f"Successfully packaged chart and saved it to: {tgz}"
            return m

        with (
            patch(
                "hyperi_ci.deployment.topology.stitch.shutil.which",
                return_value="/usr/bin/helm",
            ),
            patch(
                "hyperi_ci.deployment.topology.stitch.subprocess.run",
                side_effect=_mock_subprocess_ok,
            ),
            patch("hyperi_ci.helm.stage.subprocess.run", side_effect=_stage_subprocess),
        ):
            rc = stage.run(cfg)

        assert rc == 0
        push_calls = [c for c in recorded_stage_calls if c[:2] == ["helm", "push"]]
        assert len(push_calls) == 1, (
            f"Expected 1 helm push call, got: {recorded_stage_calls}"
        )
        assert push_calls[0][-1] == "oci://ghcr.io/hyperi-io/helm-charts"

    def test_third_party_charts_resolved_against_own_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Third-party charts are resolved against their own repository, not oci_base."""
        topo_dir = _make_topo_dir(
            tmp_path, name="full", content=_TOPOLOGY_YAML_WITH_THIRD_PARTY
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HYPERCI_PUBLISH_MODE", raising=False)

        cfg = _cfg(topo_dir)

        from hyperi_ci.deployment.topology import resolve as resolve_mod

        resolution_calls: list[tuple[str, list[str]]] = []

        def _mock_fetch(registry: str, chart_names: list[str]) -> dict[str, list[str]]:
            resolution_calls.append((registry, list(chart_names)))
            return {n: ["1.18.3" if "dfe" in n else "0.40.0"] for n in chart_names}

        monkeypatch.setattr(resolve_mod, "_fetch_available", _mock_fetch)

        dest_holder: list = []
        pkg_mock = _mock_package_subprocess(dest_holder)

        with (
            patch(
                "hyperi_ci.deployment.topology.stitch.shutil.which",
                return_value="/usr/bin/helm",
            ),
            patch(
                "hyperi_ci.deployment.topology.stitch.subprocess.run",
                side_effect=_mock_subprocess_ok,
            ),
            patch("hyperi_ci.helm.stage.subprocess.run", side_effect=pkg_mock),
        ):
            rc = stage.run(cfg)

        assert rc == 0

        # hyperi-io charts resolved against default registry
        hyperi_calls = [(r, c) for r, c in resolution_calls if "ghcr.io" in r]
        assert hyperi_calls, (
            "Expected at least one resolution call against ghcr.io registry"
        )

        # third-party chart resolved against its own repository
        tp_calls = [(r, c) for r, c in resolution_calls if "quay.io" in r]
        assert tp_calls, "Expected strimzi to be resolved against quay.io registry"
        assert any("strimzi" in chart for _, charts in tp_calls for chart in charts)
