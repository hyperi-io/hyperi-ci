# Project:   HyperI CI
# File:      tests/unit/test_config.py
# Purpose:   Tests for configuration loading and merging
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import os
from pathlib import Path

import pytest

from hyperi_ci import cli, common, staleness
from hyperi_ci import config as cfg_module
from hyperi_ci.config import (
    VALID_PROJECT_STATUSES,
    CIConfig,
    ConfigError,
    _merge_deep,
    _parse_env_value,
    load_config,
)


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

    def test_destination_for_oss(self) -> None:
        config = CIConfig(
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

    def test_destination_for_falsy_is_opt_out(self) -> None:
        # A private Python service ships only its GHCR container: python
        # is opted out with `false`, container still resolves.
        config = CIConfig(
            _raw={
                "publish": {
                    "destinations_oss": {
                        "python": False,
                        "container": "ghcr",
                    },
                },
            },
        )
        assert config.destination_for("python") == []
        assert config.destination_for("container") == ["ghcr"]

    def test_legacy_target_routes_to_oss(self) -> None:
        """A legacy ``target`` changes nothing: every publish goes to OSS."""
        config = CIConfig(
            _raw={
                "publish": {
                    "target": "internal",
                    "destinations_oss": {"python": "pypi"},
                },
            },
        )
        assert config.destination_for("python") == ["pypi"]


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

    def test_go_default_targets_are_linux_and_darwin(self, tmp_path: Path) -> None:
        """Go builds Linux and macOS by default."""
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None
        config = load_config(reload=True, project_dir=tmp_path)
        assert config.get("build.golang.targets") == [
            "linux/amd64",
            "linux/arm64",
            "darwin/amd64",
            "darwin/arm64",
        ]


class TestSectionShape:
    """A section defaults.yaml ships as a mapping takes only a mapping.

    Read any other way, every key under it would fall back to its shipped
    default: `release: false` would ship, `test: false` would hold 80% coverage.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        monkeypatch.setattr(cfg_module, "_config_cache", None)
        # `hyperi-ci check` runs this suite with HYPERCI_* set.
        for name in [n for n in os.environ if n.startswith("HYPERCI_")]:
            monkeypatch.delenv(name)
        warnings: list[str] = []
        monkeypatch.setattr(common, "warn", warnings.append)
        return warnings

    @staticmethod
    def _load(tmp_path: Path, body: str) -> CIConfig:
        (tmp_path / ".hyperi-ci.yaml").write_text(body, encoding="utf-8")
        return load_config(reload=True, project_dir=tmp_path)

    @pytest.mark.parametrize("section", ["release", "test", "quality", "build"])
    def test_an_empty_top_level_section_takes_the_shipped_defaults(
        self, tmp_path: Path, section: str, _isolated: list[str]
    ) -> None:
        config = self._load(tmp_path, f"language: rust\n{section}:\n")
        assert config.get(section) == cfg_module.shipped_default(section)
        assert _isolated == [f"`{section}:` is empty, so the shipped defaults apply"]

    def test_an_empty_release_still_releases(self, tmp_path: Path) -> None:
        config = self._load(tmp_path, "release:\n")
        assert config.setting("release.enabled") is True
        assert config.destination_for("python") == ["pypi"]

    def test_an_empty_nested_section_takes_the_shipped_defaults(
        self, tmp_path: Path, _isolated: list[str]
    ) -> None:
        body = "quality:\n  rust:\n    clippy: warn\n    feature_matrix:\n"
        config = self._load(tmp_path, body)
        assert config.setting("quality.rust.feature_matrix.enabled") is True
        assert config.get("quality.rust.clippy") == "warn"
        assert _isolated == [
            "`quality.rust.feature_matrix:` is empty, so the shipped defaults apply"
        ]

    def test_release_false_is_refused_with_the_fix(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            self._load(tmp_path, "release: false\n")
        assert str(caught.value) == (
            ".hyperi-ci.yaml: `release: false` is not a mapping -- to turn it "
            "off write `release: {enabled: false}`"
        )

    @pytest.mark.parametrize(
        "body",
        ["test: true\n", "build: 3\n", "release: off-please\n", "test: [unit]\n"],
    )
    def test_any_other_scalar_or_list_section_is_refused(
        self, tmp_path: Path, body: str
    ) -> None:
        with pytest.raises(ConfigError, match="is not a mapping -- write a mapping"):
            self._load(tmp_path, body)

    @pytest.mark.parametrize(
        ("body", "path"),
        [
            (
                "quality:\n  rust:\n    feature_matrix: false\n",
                "quality.rust.feature_matrix",
            ),
            ("release:\n  container: false\n", "release.container"),
            ("test:\n  tiers:\n    e2e: true\n", "test.tiers.e2e"),
        ],
    )
    def test_a_nested_scalar_section_is_refused(
        self, tmp_path: Path, body: str, path: str
    ) -> None:
        with pytest.raises(ConfigError, match=f"`{path}: "):
            self._load(tmp_path, body)

    def test_the_legacy_publish_spelling_is_checked_too(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="`publish: false` is not a mapping"):
            self._load(tmp_path, "publish: false\n")

    def test_every_problem_is_named_at_once(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            self._load(tmp_path, "release: false\ntest: false\n")
        assert len(str(caught.value).splitlines()) == 2

    def test_a_file_that_is_not_a_mapping_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            self._load(tmp_path, "- language: rust\n")

    def test_scalar_and_mapping_tool_modes_still_load(self, tmp_path: Path) -> None:
        """A tool ships as a bare mode, so a mode-plus-reason mapping is not a shape error."""
        body = "quality:\n  semgrep:\n    mode: disabled\n    reason: x\n  gitleaks: warn\n"
        config = self._load(tmp_path, body)
        assert config.get("quality.semgrep.reason") == "x"

    def test_an_empty_leaf_reads_as_none_not_the_shipped_value(
        self, tmp_path: Path
    ) -> None:
        """An explicit null on a LEAF is the project's value, as `get` always read it."""
        config = self._load(tmp_path, "test:\n  min_coverage:\n")
        assert config.setting("test.min_coverage") is None

    def test_setting_returns_an_explicit_none_leaf(self) -> None:
        config = CIConfig(_raw={"test": {"min_coverage": None}})
        assert config.setting("test.min_coverage") is None

    def test_the_cli_prints_the_error_and_exits_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        errors: list[str] = []
        monkeypatch.setattr(common, "error", errors.append)
        monkeypatch.setattr(staleness, "warn_if_stale", lambda: [])

        def refuse() -> None:
            raise ConfigError("a: one\nb: two")

        monkeypatch.setattr(cli, "app", refuse)
        assert cli.main() == 1
        assert errors == ["a: one", "b: two"]


class TestProjectStatus:
    """`project.status` is an information-only lifecycle stage field.

    Surfaced in CI logs and `hyperi-ci config`. Does not gate any
    behaviour. Six valid values; unknown values warn but don't fail.
    """

    def test_valid_statuses_enum(self) -> None:
        # Lock the vocabulary so a rename/typo elsewhere can't silently
        # break the contract every consumer's `.hyperi-ci.yaml` expects.
        assert VALID_PROJECT_STATUSES == (
            "experimental",
            "alpha",
            "beta",
            "ga",
            "legacy",
            "deprecated",
        )

    def test_set_status_reads_back(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "language: rust\nproject:\n  status: beta\n",
        )
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None
        config = load_config(reload=True, project_dir=tmp_path)
        assert config.get("project.status") == "beta"

    def test_unset_status_returns_empty_or_none(self, tmp_path: Path) -> None:
        # Default value in defaults.yaml is "" (empty string) -- meaning
        # "not declared". Skipping the field in `.hyperi-ci.yaml`
        # leaves the default in place.
        (tmp_path / ".hyperi-ci.yaml").write_text("language: rust\n")
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None
        config = load_config(reload=True, project_dir=tmp_path)
        # Either "" (default from defaults.yaml) or None (no key at all)
        # is acceptable -- both mean "not declared".
        status = config.get("project.status")
        assert status in ("", None)

    def test_unknown_status_warns_but_loads(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "language: rust\nproject:\n  status: stable\n",
        )
        import hyperi_ci.config as cfg_mod

        cfg_mod._config_cache = None
        config = load_config(reload=True, project_dir=tmp_path)
        # Config must still load -- typos can't break the build.
        assert config.language == "rust"
        # The unknown value is preserved in raw config so operators can
        # see what they wrote; only the log line warns.
        assert config.get("project.status") == "stable"
