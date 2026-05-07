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


def _make_config(publish_target: str = "oss") -> CIConfig:
    """Create a CIConfig with the OSS destination map populated.

    The legacy ``destinations_internal`` block was removed in v2.1.4
    along with JFrog publishing; the ``publish_target`` field is still
    accepted for back-compat with downstream ``.hyperi-ci.yaml`` files
    but is ignored at runtime.
    """
    raw = {
        "publish": {
            "target": publish_target,
            "destinations_oss": {
                "python": "pypi",
                "npm": "npmjs",
                "cargo": "crates-io",
                "container": "ghcr",
                "helm": "ghcr-charts",
                "binaries": "r2-binaries",
                "go": "go-proxy",
            },
        },
    }
    return CIConfig(publish_target=publish_target, _raw=raw)


class TestPublishDestinationRouting:
    """Verify destination_for returns OSS registries regardless of target."""

    @pytest.mark.parametrize(
        "artifact_type,expected",
        [
            ("python", ["pypi"]),
            ("npm", ["npmjs"]),
            ("cargo", ["crates-io"]),
            ("container", ["ghcr"]),
            ("helm", ["ghcr-charts"]),
            ("binaries", ["r2-binaries"]),
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

    @pytest.mark.parametrize("legacy_target", ["internal", "both"])
    def test_legacy_targets_route_to_oss(self, legacy_target: str) -> None:
        """`internal` and `both` are accepted for back-compat but route
        to OSS destinations only.
        """
        config = _make_config(legacy_target)
        assert config.destination_for("python") == ["pypi"]
        assert config.destination_for("container") == ["ghcr"]

    def test_unknown_artifact_type_returns_empty(self) -> None:
        config = _make_config("oss")
        assert config.destination_for("unknown") == []

    def test_no_destinations_configured(self) -> None:
        config = CIConfig(publish_target="oss", _raw={})
        assert config.destination_for("python") == []

    def test_empty_destinations_map(self) -> None:
        config = CIConfig(
            publish_target="oss",
            _raw={"publish": {"destinations_oss": {}}},
        )
        assert config.destination_for("python") == []


class TestPublishDestinations:
    """Verify publish_destinations returns the OSS destination map."""

    def test_oss_returns_one_map(self) -> None:
        config = _make_config("oss")
        dests = config.publish_destinations()
        assert len(dests) == 1
        assert dests[0]["python"] == "pypi"

    @pytest.mark.parametrize("legacy_target", ["internal", "both"])
    def test_legacy_targets_return_oss_map(self, legacy_target: str) -> None:
        config = _make_config(legacy_target)
        dests = config.publish_destinations()
        assert len(dests) == 1
        assert dests[0]["python"] == "pypi"

    def test_no_raw_publish_section_returns_empty(self) -> None:
        config = CIConfig(publish_target="oss", _raw={})
        assert config.publish_destinations() == []


class TestPublishTargetFromEnv:
    """``HYPERCI_PUBLISH_TARGET`` env var feeds ``publish_target`` for back-compat."""

    @pytest.mark.parametrize("value", ["oss", "internal", "both"])
    def test_env_sets_target(
        self,
        value: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None
        monkeypatch.setenv("HYPERCI_PUBLISH_TARGET", value)
        config = cfg_mod.load_config(reload=True, project_dir=tmp_path)
        assert config.publish_target == value


class TestOSSDestinationHygiene:
    """OSS destinations must never accidentally point at JFrog."""

    def test_oss_destinations_are_not_jfrog(self) -> None:
        config = _make_config("oss")
        dests = config.publish_destinations()
        for artifact_type, destination in dests[0].items():
            assert "jfrog" not in destination, (
                f"OSS destination for '{artifact_type}' points to JFrog: "
                f"'{destination}'"
            )


class TestChannelRouting:
    """Channel determines GH Release flags and R2 paths."""

    def test_release_channel_no_prerelease(self) -> None:
        from hyperi_ci.publish.binaries import _resolve_gh_release_flags

        flags = _resolve_gh_release_flags("release")
        assert "--prerelease" not in flags

    def test_alpha_channel_prerelease(self) -> None:
        from hyperi_ci.publish.binaries import _resolve_gh_release_flags

        flags = _resolve_gh_release_flags("alpha")
        assert "--prerelease" in flags

    def test_spike_channel_prerelease(self) -> None:
        from hyperi_ci.publish.binaries import _resolve_gh_release_flags

        flags = _resolve_gh_release_flags("spike")
        assert "--prerelease" in flags

    def test_beta_channel_prerelease(self) -> None:
        from hyperi_ci.publish.binaries import _resolve_gh_release_flags

        flags = _resolve_gh_release_flags("beta")
        assert "--prerelease" in flags

    def test_release_r2_path(self) -> None:
        from hyperi_ci.publish.binaries import _resolve_r2_paths

        versioned, latest = _resolve_r2_paths("dfe-receiver", "1.3.0", "release")
        assert versioned.endswith("/dfe-receiver/v1.3.0/")
        assert latest.endswith("/dfe-receiver/latest/")
        assert "/release/" not in versioned

    def test_alpha_r2_path(self) -> None:
        from hyperi_ci.publish.binaries import _resolve_r2_paths

        versioned, latest = _resolve_r2_paths("dfe-receiver", "1.3.0", "alpha")
        assert "/alpha/" in versioned
        assert "/alpha/" in latest

    def test_beta_r2_path(self) -> None:
        from hyperi_ci.publish.binaries import _resolve_r2_paths

        versioned, latest = _resolve_r2_paths("dfe-receiver", "1.3.0", "beta")
        assert "/beta/" in versioned
        assert "/beta/" in latest

    def test_spike_r2_path(self) -> None:
        from hyperi_ci.publish.binaries import _resolve_r2_paths

        versioned, latest = _resolve_r2_paths("dfe-receiver", "1.3.0", "spike")
        assert "/spike/" in versioned
        assert "/spike/" in latest
