# Project:   HyperI CI
# File:      tests/unit/test_publish.py
# Purpose:   Tests for publish destination routing (no mocks)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Publish destination routing tests.

Tests the config-level routing logic that determines WHERE artifacts
are published based on publish_target. Does NOT test actual publishing
(subprocess calls to uv/cargo/npm) — that requires real registries
and is tested via integration tests against test projects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig


def _make_config(publish_target: str = "internal") -> CIConfig:
    """Create a CIConfig with full destination mappings."""
    raw = {
        "publish": {
            "target": publish_target,
            "destinations_internal": {
                "python": "jfrog-pypi",
                "npm": "jfrog-npm",
                "cargo": "jfrog-cargo",
                "container": "jfrog-docker",
                "helm": "jfrog-helm",
                "binaries": "jfrog-generic",
                "go": "jfrog-go",
            },
            "destinations_oss": {
                "python": "pypi",
                "npm": "npmjs",
                "cargo": "crates-io",
                "container": "ghcr",
                "helm": "ghcr-charts",
                "binaries": "github-releases",
                "go": "go-proxy",
            },
        },
    }
    return CIConfig(publish_target=publish_target, _raw=raw)


class TestPublishDestinationRouting:
    """Verify destination_for returns correct registries per target."""

    @pytest.mark.parametrize(
        "artifact_type,expected",
        [
            ("python", ["jfrog-pypi"]),
            ("npm", ["jfrog-npm"]),
            ("cargo", ["jfrog-cargo"]),
            ("container", ["jfrog-docker"]),
            ("helm", ["jfrog-helm"]),
            ("binaries", ["jfrog-generic"]),
            ("go", ["jfrog-go"]),
        ],
    )
    def test_internal_routes_to_jfrog(
        self,
        artifact_type: str,
        expected: list[str],
    ) -> None:
        config = _make_config("internal")
        assert config.destination_for(artifact_type) == expected

    @pytest.mark.parametrize(
        "artifact_type,expected",
        [
            ("python", ["pypi"]),
            ("npm", ["npmjs"]),
            ("cargo", ["crates-io"]),
            ("container", ["ghcr"]),
            ("helm", ["ghcr-charts"]),
            ("binaries", ["github-releases"]),
            ("go", ["go-proxy"]),
        ],
    )
    def test_oss_routes_to_public(
        self,
        artifact_type: str,
        expected: list[str],
    ) -> None:
        config = _make_config("oss")
        assert config.destination_for(artifact_type) == expected

    @pytest.mark.parametrize(
        "artifact_type,internal,oss",
        [
            ("python", "jfrog-pypi", "pypi"),
            ("npm", "jfrog-npm", "npmjs"),
            ("cargo", "jfrog-cargo", "crates-io"),
            ("container", "jfrog-docker", "ghcr"),
            ("helm", "jfrog-helm", "ghcr-charts"),
            ("binaries", "jfrog-generic", "github-releases"),
            ("go", "jfrog-go", "go-proxy"),
        ],
    )
    def test_both_routes_to_both(
        self,
        artifact_type: str,
        internal: str,
        oss: str,
    ) -> None:
        config = _make_config("both")
        destinations = config.destination_for(artifact_type)
        assert destinations == [internal, oss]

    def test_unknown_artifact_type_returns_empty(self) -> None:
        config = _make_config("internal")
        assert config.destination_for("unknown") == []

    def test_no_destinations_configured(self) -> None:
        config = CIConfig(publish_target="internal", _raw={})
        assert config.destination_for("python") == []

    def test_empty_destinations_map(self) -> None:
        config = CIConfig(
            publish_target="oss",
            _raw={"publish": {"destinations_oss": {}}},
        )
        assert config.destination_for("python") == []


class TestPublishDestinations:
    """Verify publish_destinations returns correct destination maps."""

    def test_internal_returns_one_map(self) -> None:
        config = _make_config("internal")
        dests = config.publish_destinations()
        assert len(dests) == 1
        assert dests[0]["python"] == "jfrog-pypi"

    def test_oss_returns_one_map(self) -> None:
        config = _make_config("oss")
        dests = config.publish_destinations()
        assert len(dests) == 1
        assert dests[0]["python"] == "pypi"

    def test_both_returns_two_maps(self) -> None:
        config = _make_config("both")
        dests = config.publish_destinations()
        assert len(dests) == 2
        assert dests[0]["python"] == "jfrog-pypi"
        assert dests[1]["python"] == "pypi"

    def test_invalid_target_returns_empty(self) -> None:
        config = CIConfig(publish_target="nonexistent", _raw={})
        assert config.publish_destinations() == []

    def test_no_raw_publish_section_returns_empty_map(self) -> None:
        config = CIConfig(publish_target="internal", _raw={})
        dests = config.publish_destinations()
        assert len(dests) == 1
        assert dests[0] == {}


class TestPublishTargetFromEnv:
    """Verify HYPERCI_PUBLISH_TARGET env var overrides config."""

    def test_env_sets_oss(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None
        monkeypatch.setenv("HYPERCI_PUBLISH_TARGET", "oss")
        config = cfg_mod.load_config(reload=True, project_dir=tmp_path)
        assert config.publish_target == "oss"

    def test_env_sets_both(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None
        monkeypatch.setenv("HYPERCI_PUBLISH_TARGET", "both")
        config = cfg_mod.load_config(reload=True, project_dir=tmp_path)
        assert config.publish_target == "both"

    def test_env_sets_internal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None
        monkeypatch.setenv("HYPERCI_PUBLISH_TARGET", "internal")
        config = cfg_mod.load_config(reload=True, project_dir=tmp_path)
        assert config.publish_target == "internal"


class TestOSSReadiness:
    """Verify OSS destinations are complete and ready for JFrog cutover.

    When publish_target switches from 'internal' to 'oss', every
    artifact type that has a JFrog destination MUST have a corresponding
    OSS destination. This test ensures no gaps exist.
    """

    def test_every_internal_type_has_oss_equivalent(self) -> None:
        config = _make_config("both")
        dests = config.publish_destinations()
        internal_map = dests[0]
        oss_map = dests[1]
        for artifact_type in internal_map:
            assert artifact_type in oss_map, (
                f"Artifact type '{artifact_type}' has internal destination "
                f"'{internal_map[artifact_type]}' but no OSS destination"
            )

    def test_oss_destinations_are_not_jfrog(self) -> None:
        config = _make_config("oss")
        dests = config.publish_destinations()
        oss_map = dests[0]
        for artifact_type, destination in oss_map.items():
            assert "jfrog" not in destination, (
                f"OSS destination for '{artifact_type}' points to JFrog: "
                f"'{destination}'"
            )

    def test_internal_destinations_are_jfrog(self) -> None:
        config = _make_config("internal")
        dests = config.publish_destinations()
        internal_map = dests[0]
        for artifact_type, destination in internal_map.items():
            assert "jfrog" in destination, (
                f"Internal destination for '{artifact_type}' does not point "
                f"to JFrog: '{destination}'"
            )
