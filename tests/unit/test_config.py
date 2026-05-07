# Project:   HyperI CI
# File:      tests/unit/test_config.py
# Purpose:   Tests for configuration loading and merging
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig, _merge_deep, _parse_env_value, load_config


class TestMergeDeep:
    """Deep merge of configuration dicts."""

    def test_simple_override(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 3}
        assert _merge_deep(base, override) == {"a": 1, "b": 3}

    def test_nested_merge(self) -> None:
        base = {"quality": {"python": {"ruff": "blocking"}}}
        override = {"quality": {"python": {"pyright": "warn"}}}
        result = _merge_deep(base, override)
        assert result["quality"]["python"]["ruff"] == "blocking"
        assert result["quality"]["python"]["pyright"] == "warn"

    def test_override_replaces_non_dict(self) -> None:
        base = {"a": [1, 2]}
        override = {"a": [3, 4]}
        assert _merge_deep(base, override) == {"a": [3, 4]}


class TestParseEnvValue:
    """Environment variable value parsing."""

    def test_true_values(self) -> None:
        for v in ("true", "True", "yes", "1"):
            assert _parse_env_value(v) is True

    def test_false_values(self) -> None:
        for v in ("false", "False", "no", "0"):
            assert _parse_env_value(v) is False

    def test_integer(self) -> None:
        assert _parse_env_value("42") == 42

    def test_json_list(self) -> None:
        assert _parse_env_value('["a", "b"]') == ["a", "b"]

    def test_plain_string(self) -> None:
        assert _parse_env_value("hello") == "hello"


class TestCIConfig:
    """CIConfig dot-notation access."""

    def test_get_nested_value(self) -> None:
        config = CIConfig(_raw={"quality": {"python": {"ruff": "blocking"}}})
        assert config.get("quality.python.ruff") == "blocking"

    def test_get_missing_returns_default(self) -> None:
        config = CIConfig(_raw={})
        assert config.get("quality.python.ruff", "warn") == "warn"

    def test_get_top_level(self) -> None:
        config = CIConfig(_raw={"language": "rust"})
        assert config.get("language") == "rust"

    def test_publish_target_defaults_to_oss(self) -> None:
        config = CIConfig(_raw={})
        assert config.publish_target == "oss"

    def test_destination_for_oss(self) -> None:
        config = CIConfig(
            publish_target="oss",
            _raw={
                "publish": {
                    "destinations_oss": {
                        "python": "pypi",
                        "container": "ghcr",
                    },
                },
            },
        )
        assert config.destination_for("python") == ["pypi"]
        assert config.destination_for("container") == ["ghcr"]

    def test_legacy_target_internal_routes_to_oss(self) -> None:
        """Legacy ``target: internal`` is accepted for back-compat but
        ignored — every publish goes to OSS destinations.
        """
        config = CIConfig(
            publish_target="internal",
            _raw={
                "publish": {
                    "destinations_oss": {"python": "pypi"},
                },
            },
        )
        assert config.destination_for("python") == ["pypi"]

    def test_legacy_target_both_routes_to_oss(self) -> None:
        """Legacy ``target: both`` is accepted for back-compat but
        treated as OSS since JFrog publishing was removed in v2.1.4.
        """
        config = CIConfig(
            publish_target="both",
            _raw={
                "publish": {
                    "destinations_oss": {"python": "pypi"},
                },
            },
        )
        assert config.destination_for("python") == ["pypi"]

    def test_publish_target_from_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None
        monkeypatch.setenv("HYPERCI_PUBLISH_TARGET", "oss")
        config = load_config(reload=True, project_dir=tmp_path)
        assert config.publish_target == "oss"


class TestLoadConfig:
    """Full config loading with file cascade."""

    def test_loads_project_config(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "language: rust\nquality:\n  enabled: false\n",
        )
        # Reset cache
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None

        config = load_config(reload=True, project_dir=tmp_path)
        assert config.language == "rust"
        assert config.get("quality.enabled") is False

    def test_env_var_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None

        monkeypatch.setenv("HYPERCI_LANGUAGE", "golang")
        config = load_config(reload=True, project_dir=tmp_path)
        assert config.get("language") == "golang"
