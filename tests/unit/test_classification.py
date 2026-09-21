# Project:   HyperI CI
# File:      tests/unit/test_classification.py
# Purpose:   Tests for repo-classification parsing, fallback and resolution
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""The declared repo category decides visibility, licence and publishing.

The case that matters most is the absent one: a repo that declares
nothing must read as `internal`, never as `general-oss`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci import classification
from hyperi_ci.config import load_config


def _fresh_load(project_dir: Path):
    """Load config with the module-level cache cleared."""
    import hyperi_ci.config as cfg_mod

    cfg_mod._config_cache = None
    return load_config(reload=True, project_dir=project_dir)


class TestNormalise:
    """Canonical values and the aliases that normalise to them."""

    def test_canonical_vocabulary_is_locked(self) -> None:
        # The only four values the GitHub org custom property accepts —
        # a rename here silently drifts the repo marker from the org.
        assert classification.CANONICAL == (
            "internal",
            "product",
            "fork",
            "general-oss",
        )

    @pytest.mark.parametrize("value", classification.CANONICAL)
    def test_canonical_value_round_trips(self, value: str) -> None:
        assert classification.normalise(value) == value

    @pytest.mark.parametrize(
        ("alias", "expected"),
        [
            ("hyperi", "internal"),
            ("oss", "general-oss"),
            ("general_oss", "general-oss"),
            ("generaloss", "general-oss"),
            ("1", "internal"),
            ("2", "product"),
            ("3", "fork"),
            ("4", "general-oss"),
        ],
    )
    def test_alias_normalises(self, alias: str, expected: str) -> None:
        assert classification.normalise(alias) == expected

    def test_case_and_whitespace_are_forgiven(self) -> None:
        assert classification.normalise("  General-OSS \n") == "general-oss"

    def test_yaml_integer_normalises(self) -> None:
        # `classification: 2` parses as an int, not a string.
        assert classification.normalise(2) == "product"

    def test_unknown_value_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown classification"):
            classification.normalise("public")


class TestDotfile:
    """`.hyperi-classification` is the fallback for repos with no yaml."""

    def test_absent_file_is_none(self, tmp_path: Path) -> None:
        assert classification.read_dotfile(tmp_path) is None

    def test_token_is_read(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-classification").write_text("fork\n", encoding="utf-8")
        assert classification.read_dotfile(tmp_path) == "fork"

    def test_comments_and_blanks_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-classification").write_text(
            "# carried in a non-upstreamed commit\n\nproduct\n",
            encoding="utf-8",
        )
        assert classification.read_dotfile(tmp_path) == "product"

    def test_empty_file_is_none(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-classification").write_text("\n", encoding="utf-8")
        assert classification.read_dotfile(tmp_path) is None

    def test_invalid_token_raises(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-classification").write_text("private\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Unknown classification"):
            classification.read_dotfile(tmp_path)


class TestOrgProperty:
    """The third rung: what the GitHub org declares for the repo."""

    def test_live_payload_shape_parses(self) -> None:
        # Verbatim body of `gh api repos/hyperi-io/hyperi-ci/properties/values`.
        payload = (
            '[{"property_name":"ci","value":"custom"},'
            '{"property_name":"classification","value":"general-oss"},'
            '{"property_name":"steward","value":"hyperi"}]'
        )
        assert classification.parse_org_properties(payload) == "general-oss"

    def test_no_classification_property_is_none(self) -> None:
        assert classification.parse_org_properties('[{"property_name":"ci"}]') is None

    def test_unreadable_payload_raises(self) -> None:
        with pytest.raises(ValueError, match="Unreadable org-property payload"):
            classification.parse_org_properties("not json")


class TestResolve:
    """Precedence across the in-repo markers."""

    def test_config_wins_over_dotfile(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-classification").write_text("fork\n", encoding="utf-8")
        result = classification.resolve({"classification": "product"}, tmp_path)
        assert result.value == "product"
        assert result.source == classification.SOURCE_CONFIG

    def test_dotfile_is_the_fallback(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-classification").write_text("fork\n", encoding="utf-8")
        result = classification.resolve({}, tmp_path)
        assert result.value == "fork"
        assert result.source == classification.SOURCE_DOTFILE

    def test_empty_config_value_falls_through(self, tmp_path: Path) -> None:
        # defaults.yaml ships `classification: ""` — that is "not declared",
        # so it must not shadow the dotfile.
        (tmp_path / ".hyperi-classification").write_text("fork\n", encoding="utf-8")
        assert classification.resolve({"classification": ""}, tmp_path).value == "fork"

    def test_absent_on_both_is_most_restrictive(self, tmp_path: Path) -> None:
        result = classification.resolve({}, tmp_path)
        assert result.value == ""
        assert result.source == classification.SOURCE_UNDECLARED
        assert result.effective == "internal"


class TestLoadConfigIntegration:
    """`hyperi-ci config` reads these three keys off the merged config."""

    def test_declared_in_yaml(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "language: rust\nclassification: product\n",
            encoding="utf-8",
        )
        config = _fresh_load(tmp_path)
        assert config.classification == "product"
        assert config.classification_source == ".hyperi-ci.yaml"
        assert config.classification_effective == "product"
        assert config.get("classification") == "product"

    def test_alias_in_yaml_is_normalised(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "classification: oss\n",
            encoding="utf-8",
        )
        assert _fresh_load(tmp_path).classification == "general-oss"

    def test_dotfile_fallback_when_yaml_is_silent(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("language: rust\n", encoding="utf-8")
        (tmp_path / ".hyperi-classification").write_text("fork\n", encoding="utf-8")
        config = _fresh_load(tmp_path)
        assert config.classification == "fork"
        assert config.classification_source == ".hyperi-classification"

    def test_absent_on_both_never_reads_as_oss(self, tmp_path: Path) -> None:
        config = _fresh_load(tmp_path)
        assert config.classification == ""
        assert config.classification_source == "undeclared"
        assert config.classification_effective == "internal"
        assert config.get("classification_effective") == "internal"

    def test_invalid_value_warns_and_downgrades(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "language: rust\nclassification: public\n",
            encoding="utf-8",
        )
        config = _fresh_load(tmp_path)
        # The config still loads — a typo cannot break the build.
        assert config.language == "rust"
        # But it is not honoured: an unknown category is no declaration.
        assert config.classification == ""
        assert config.classification_effective == "internal"

    def test_env_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HYPERCI_CLASSIFICATION", "fork")
        assert _fresh_load(tmp_path).classification == "fork"
