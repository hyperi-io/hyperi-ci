# Project:   HyperI CI
# File:      tests/unit/test_publish.py
# Purpose:   Tests for publish destination routing (no mocks)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Publish destination routing tests.

Tests the config-level routing logic that determines WHERE artifacts
are published based on publish_target. Does NOT test actual publishing
(subprocess calls to uv/cargo/npm) — that requires real registries
and is tested via integration tests against test projects.
"""

from __future__ import annotations

import subprocess
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


class TestCargoVersionSync:
    """Publish's Cargo.toml stamp shares the build's table-scoped stamper —
    no duplicate regex, and a dependency `version =` is never clobbered."""

    def test_table_scoped_leaves_dependency_version_alone(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # A dependency table with its own `version =` line BEFORE [package].
        # The old unscoped count=1 regex would stamp tokio's version; the
        # shared table-scoped stamper must only touch [package].
        (tmp_path / "Cargo.toml").write_text(
            '[dependencies.tokio]\nversion = "1.35"\n\n'
            '[package]\nname = "x"\nversion = "0.0.0"\n'
        )
        from hyperi_ci.languages.rust.publish import _sync_cargo_toml_version

        assert _sync_cargo_toml_version("9.9.9") is True
        txt = (tmp_path / "Cargo.toml").read_text()
        assert '[package]\nname = "x"\nversion = "9.9.9"' in txt
        assert 'version = "1.35"' in txt  # tokio dependency untouched

    def test_missing_cargo_toml_is_error(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        from hyperi_ci.languages.rust.publish import _sync_cargo_toml_version

        assert _sync_cargo_toml_version("1.0.0") is False


class TestPythonDistExclusion:
    """destinations_oss.python: false must also stop the wheel reaching R2
    via the generic binary publisher (issue #105 BUG 2)."""

    def test_wheel_and_sdist_are_python_artifacts(self) -> None:
        from hyperi_ci.publish.binaries import _is_python_dist_artifact

        assert _is_python_dist_artifact(Path("dfe_engine-1.17.0-py3-none-any.whl"))
        assert _is_python_dist_artifact(Path("dfe_engine-1.17.0.tar.gz"))
        assert _is_python_dist_artifact(Path("dfe_engine-1.17.0.zip"))

    def test_binary_is_not_python_artifact(self) -> None:
        from hyperi_ci.publish.binaries import _is_python_dist_artifact

        assert not _is_python_dist_artifact(Path("dfe-receiver"))
        assert not _is_python_dist_artifact(Path("dfe-receiver-x86_64-unknown-linux"))

    def test_collect_excludes_python_when_flagged(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "dfe_engine-1.17.0-py3-none-any.whl").write_text("wheel")
        (dist / "dfe_engine-1.17.0.tar.gz").write_text("sdist")
        (dist / "dfe-receiver").write_text("binary")
        from hyperi_ci.publish.binaries import _collect_artifacts

        kept = {p.name for p in _collect_artifacts(exclude_python=True)}
        assert kept == {"dfe-receiver"}
        # Default (no exclusion) still sweeps in everything.
        assert "dfe_engine-1.17.0-py3-none-any.whl" in {
            p.name for p in _collect_artifacts()
        }

    def test_python_false_opts_out_but_keeps_binaries(self) -> None:
        # The dfe-engine shape: python opted out, binaries still defaulted on.
        config = CIConfig(
            publish_target="oss",
            _raw={
                "publish": {
                    "destinations_oss": {"python": False, "binaries": "r2-binaries"}
                }
            },
        )
        assert config.destination_for("python") == []
        assert config.destination_for("binaries") == ["r2-binaries"]
        # publish_binaries derives exclude_python from exactly this.
        assert bool(config.destination_for("python")) is False


class TestReleaseTargetsHead:
    """Refuse to overwrite a release whose tag is not HEAD (issue #105)."""

    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    def test_tag_at_head_is_true(self, tmp_path, monkeypatch) -> None:
        self._git(tmp_path, "init")
        (tmp_path / "f").write_text("a")
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-m", "one")
        self._git(tmp_path, "tag", "v1.0.0")
        monkeypatch.chdir(tmp_path)
        from hyperi_ci.publish.binaries import _release_targets_head

        assert _release_targets_head("v1.0.0") is True

    def test_tag_off_head_is_false(self, tmp_path, monkeypatch) -> None:
        self._git(tmp_path, "init")
        (tmp_path / "f").write_text("a")
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-m", "one")
        self._git(tmp_path, "tag", "v1.0.0")
        (tmp_path / "f").write_text("b")
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-m", "two")
        monkeypatch.chdir(tmp_path)
        from hyperi_ci.publish.binaries import _release_targets_head

        assert _release_targets_head("v1.0.0") is False

    def test_missing_tag_is_false(self, tmp_path, monkeypatch) -> None:
        self._git(tmp_path, "init")
        (tmp_path / "f").write_text("a")
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-m", "one")
        monkeypatch.chdir(tmp_path)
        from hyperi_ci.publish.binaries import _release_targets_head

        assert _release_targets_head("v9.9.9") is False
