# Project:   HyperI CI
# File:      tests/unit/test_vocabulary.py
# Purpose:   One word for the release event, and the old spellings still working
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""issue #149: `release` is the one word, and nothing written before it breaks.

Back-compat is the whole point of the rename, so most of this file is about the
old spellings continuing to work.
"""

import pytest

from hyperi_ci import config as config_module
from hyperi_ci import vocabulary
from hyperi_ci.config import CIConfig, load_config, packaged_default


class TestFoldLegacyConfig:
    """`publish:` folds into `release:` before the defaults merge."""

    def test_a_legacy_block_becomes_the_canonical_one(self) -> None:
        doc, keys = vocabulary.fold_legacy_config(
            {"publish": {"channel": "beta", "enabled": True}}
        )
        assert doc["release"] == {"channel": "beta", "enabled": True}
        assert "publish" not in doc
        assert keys == ["publish.channel", "publish.enabled"]

    def test_the_canonical_spelling_wins_a_conflict(self) -> None:
        # One file, both spellings: the author has already said which they mean.
        doc, _ = vocabulary.fold_legacy_config(
            {"publish": {"channel": "alpha"}, "release": {"channel": "beta"}}
        )
        assert doc["release"]["channel"] == "beta"

    def test_the_fold_is_deep(self) -> None:
        doc, _ = vocabulary.fold_legacy_config(
            {
                "publish": {"container": {"enabled": True, "port": 8080}},
                "release": {"container": {"port": 9000}},
            }
        )
        assert doc["release"]["container"] == {"enabled": True, "port": 9000}

    def test_no_legacy_block_changes_nothing(self) -> None:
        original = {"release": {"channel": "release"}}
        doc, keys = vocabulary.fold_legacy_config(original)
        assert doc == original
        assert keys == []

    def test_a_non_mapping_is_returned_untouched(self) -> None:
        assert vocabulary.fold_legacy_config(None) == (None, [])


class TestKeyAliasing:
    """Both directions, because a config reaches a reader folded or raw."""

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("publish.channel", "release.channel"),
            ("publish", "release"),
            ("release.channel", "release.channel"),
            ("quality.ruff", "quality.ruff"),
        ],
    )
    def test_canonical_key(self, key: str, expected: str) -> None:
        assert vocabulary.canonical_key(key) == expected

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("release.channel", "publish.channel"),
            ("release", "publish"),
            ("quality.ruff", "quality.ruff"),
        ],
    )
    def test_legacy_key(self, key: str, expected: str) -> None:
        assert vocabulary.legacy_key(key) == expected

    def test_candidates_try_the_asked_for_spelling_first(self) -> None:
        # A caller holding an unfolded dict must get its own value, not a
        # default, so the literal key is tried before any rewrite.
        assert vocabulary.key_candidates("publish.channel")[0] == "publish.channel"
        assert "release.channel" in vocabulary.key_candidates("publish.channel")

    def test_candidates_are_deduplicated(self) -> None:
        assert vocabulary.key_candidates("quality.ruff") == ["quality.ruff"]


class TestReleaseTrailer:
    """The composite matches the same set in shell; these must agree."""

    @pytest.mark.parametrize(
        "message",
        [
            "fix: thing\n\nRelease: true\n",
            "fix: thing\n\nPublish: true\n",
            "fix: thing\n\nrelease: true\n",
            "fix: thing\n\nRELEASE: TRUE\n",
            "fix: thing\n\n  Release:   true  \n",
        ],
    )
    def test_accepted(self, message: str) -> None:
        assert vocabulary.has_release_trailer(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "fix: thing\n",
            "fix: thing\n\nRelease: false\n",
            "fix: thing\n\nReleased-by: someone\n",
            "fix: thing\n\nPublished-by: someone\n",
        ],
    )
    def test_rejected(self, message: str) -> None:
        assert vocabulary.has_release_trailer(message) is False


class TestTrailerValues:
    """One reader for every trailer, so the release trailer has no private parser."""

    @staticmethod
    def _read(message: str, key: str = "Release") -> list[str]:
        return vocabulary.trailer_values(message, key)

    def test_absent_is_empty(self) -> None:
        assert self._read("fix: thing\n") == []

    def test_the_value_comes_back(self) -> None:
        assert self._read("fix: thing\n\nRelease: true\n") == ["true"]

    def test_the_key_is_case_insensitive(self) -> None:
        assert self._read("fix: x\n\nrELEASE: true\n") == ["true"]

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert self._read("fix: x\n\n  Release:   true  \n") == ["true"]

    def test_every_occurrence_in_order(self) -> None:
        assert self._read("fix: x\n\nRelease: false\n\nRelease: true\n") == [
            "false",
            "true",
        ]

    def test_a_longer_key_is_not_a_match(self) -> None:
        assert self._read("fix: x\n\nReleased-by: someone\n") == []


class TestDeprecationMessage:
    """A renamed key and a removed key have different futures; say so."""

    def test_a_renamed_key_names_its_replacement(self) -> None:
        message = vocabulary.deprecated_config_message(["publish.channel"])
        assert "publish.channel -> release.channel" in message
        assert "keeps working" in message
        assert "REMOVED" not in message

    def test_a_legacy_destination_key_names_the_removal_date(self) -> None:
        message = vocabulary.deprecated_config_message(["publish.destinations_oss"])
        assert "REMOVED" in message
        assert vocabulary.REMOVAL_DATE in message

    def test_destinations_oss_says_move_it_not_delete_it(self) -> None:
        """Deleting it turns every destination it opted out back on."""
        message = vocabulary.deprecated_config_message(["publish.destinations_oss"])
        assert "publish.destinations_oss -> release.destinations" in message
        assert "inert" not in message
        assert "Delete" not in message

    @pytest.mark.parametrize(
        "doc",
        [
            {"release": {"destinations_oss": {"python": False}}},
            {"release": {"target": "oss"}, "publish": {"channel": "beta"}},
        ],
        ids=["release-block-only", "beside-a-publish-block"],
    )
    def test_a_removal_key_under_release_is_reported_too(self, doc) -> None:
        """Written under release:, it once drew no warning at all."""
        _, keys = vocabulary.fold_legacy_config(doc)
        key = next(iter(doc["release"]))
        assert f"release.{key}" in keys

    def test_load_config_names_a_release_block_removal_key(
        self, tmp_path, monkeypatch
    ) -> None:
        """The path a real run takes, not only the fold helper."""
        monkeypatch.setattr(config_module, "_config_cache", None)
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "release:\n  destinations_oss:\n    python: false\n", encoding="utf-8"
        )
        config = load_config(reload=True, project_dir=tmp_path)
        assert "release.destinations_oss" in config.deprecated_keys
        assert config.destination_for("python") == []

    def test_target_is_still_told_to_go(self) -> None:
        message = vocabulary.deprecated_config_message(["publish.target"])
        assert "Delete" in message
        assert "release.destinations" not in message

    def test_moving_keeps_an_opt_out_that_deleting_loses(self) -> None:
        """The reason the advice is move: npm publishing comes back on delete."""
        shipped = dict(packaged_default("release.destinations"))
        opted_out = CIConfig(
            _raw={
                "release": {"destinations": shipped},
                "publish": {"destinations_oss": {"npm": False}},
            }
        )
        moved = CIConfig(_raw={"release": {"destinations": {**shipped, "npm": False}}})
        deleted = CIConfig(_raw={"release": {"destinations": shipped}})
        assert opted_out.destination_for("npm") == []
        assert moved.destination_for("npm") == []
        assert deleted.destination_for("npm") != []

    def test_both_tiers_are_reported_separately(self) -> None:
        message = vocabulary.deprecated_config_message(
            ["publish.channel", "publish.target"]
        )
        assert "publish.channel -> release.channel" in message
        assert "REMOVED" in message

    def test_the_reversal_is_stated_rather_than_left_to_look_like_a_flip_flop(
        self,
    ) -> None:
        assert "withdrawn" in vocabulary.REVERSAL_NOTE
        assert "release" in vocabulary.REVERSAL_NOTE


class TestConfigReadsBothSpellings:
    """A raw config was never folded, so `get` must still answer it."""

    def test_a_legacy_raw_config_resolves_through_the_new_name(self) -> None:
        config = CIConfig(_raw={"publish": {"channel": "beta"}})
        assert config.get("release.channel") == "beta"

    def test_a_folded_config_still_answers_the_old_name(self) -> None:
        config = CIConfig(_raw={"release": {"channel": "beta"}})
        assert config.get("publish.channel") == "beta"

    def test_an_unrelated_key_is_untouched(self) -> None:
        config = CIConfig(_raw={"quality": {"ruff": "blocking"}})
        assert config.get("quality.ruff") == "blocking"
        assert config.get("quality.missing", "fallback") == "fallback"

    def test_destinations_resolves_under_either_key(self) -> None:
        # `destinations_oss` folds to `release.destinations_oss`, which is not
        # the canonical `release.destinations` -- the fallback covers it.
        legacy = CIConfig(_raw={"publish": {"destinations_oss": {"python": "pypi"}}})
        assert legacy.destination_for("python") == ["pypi"]
        current = CIConfig(_raw={"release": {"destinations": {"python": "pypi"}}})
        assert current.destination_for("python") == ["pypi"]


class TestRemovedKeys:
    """A key hyperi-ci no longer reads warns once and never fails the build."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vocabulary, "_reported_removed_keys", set())
        monkeypatch.setattr(config_module, "_config_cache", None)

    @staticmethod
    def _announced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from hyperi_ci import common

        seen: list[str] = []
        monkeypatch.setattr(
            common,
            "announce",
            lambda msg, title, *, level="warning": seen.append(msg),
        )
        return seen

    def _load(self, tmp_path, monkeypatch, body: str) -> list[str]:
        seen = self._announced(monkeypatch)
        (tmp_path / ".hyperi-ci.yaml").write_text(body, encoding="utf-8")
        load_config(reload=True, project_dir=tmp_path)
        return seen

    def test_a_removed_key_warns_once(self, tmp_path, monkeypatch) -> None:
        seen = self._load(
            tmp_path,
            monkeypatch,
            "language: python\nrelease:\n  helm:\n    enabled: true\n"
            "    chart_path: chart\n",
        )
        assert seen == [
            "release.helm is no longer read by hyperi-ci and can be deleted"
        ]

    def test_a_reload_does_not_repeat_the_warning(self, tmp_path, monkeypatch) -> None:
        seen = self._load(tmp_path, monkeypatch, "build:\n  type: app\n")
        load_config(reload=True, project_dir=tmp_path)
        load_config(reload=True, project_dir=tmp_path)
        assert seen == [vocabulary.removed_key_message("build.type")]

    def test_a_current_key_does_not_warn(self, tmp_path, monkeypatch) -> None:
        seen = self._load(
            tmp_path,
            monkeypatch,
            "language: python\nrelease:\n  destinations:\n    python: false\n"
            "  container:\n    enabled: auto\n",
        )
        assert seen == []

    @pytest.mark.parametrize(
        ("body", "key"),
        [
            ("typescript:\n  package_manager: yarn\n", "typescript.package_manager"),
            ("golang:\n  targets: [linux/amd64]\n", "golang"),
            ("deployment:\n  producer: false\n", "deployment"),
            ("release:\n  container:\n    mode: template\n", "release.container.mode"),
        ],
    )
    def test_top_level_and_container_keys_warn(
        self, tmp_path, monkeypatch, body: str, key: str
    ) -> None:
        assert self._load(tmp_path, monkeypatch, body) == [
            vocabulary.removed_key_message(key)
        ]

    def test_the_legacy_spelling_is_named_as_written(self) -> None:
        found = vocabulary.find_removed_keys({"publish": {"binaries": "both"}})
        assert found == ["publish.binaries"]

    def test_a_removed_legacy_key_gets_no_rename_notice(
        self, tmp_path, monkeypatch
    ) -> None:
        self._announced(monkeypatch)
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "publish:\n  helm:\n    enabled: true\n  channel: beta\n",
            encoding="utf-8",
        )
        config = load_config(reload=True, project_dir=tmp_path)
        assert config.deprecated_keys == ["publish.channel"]

    def test_a_partly_removed_block_keeps_its_rename_notice(
        self, tmp_path, monkeypatch
    ) -> None:
        self._announced(monkeypatch)
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "publish:\n  container:\n    registry: ghcr.io\n    enabled: auto\n",
            encoding="utf-8",
        )
        config = load_config(reload=True, project_dir=tmp_path)
        assert config.deprecated_keys == ["publish.container"]

    def test_a_removed_key_does_not_fail_the_load(self, tmp_path, monkeypatch) -> None:
        self._announced(monkeypatch)
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "language: rust\nbuild:\n  type: app\n", encoding="utf-8"
        )
        config = load_config(reload=True, project_dir=tmp_path)
        assert config.language == "rust"

    def test_config_json_stays_parseable(self, tmp_path, monkeypatch) -> None:
        """Under GitHub Actions the annotation goes to stdout, so --json skips it."""
        import json

        from typer.testing import CliRunner

        from hyperi_ci.cli import app

        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "language: rust\nrunners:\n  default: arc\n", encoding="utf-8"
        )
        result = CliRunner().invoke(app, ["config", "--json", "-C", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["runners"] == {"default": "arc"}

    def test_every_removed_key_is_absent_from_the_shipped_defaults(self) -> None:
        """A removed key the defaults still ship would read as a live knob."""
        for key in vocabulary.REMOVED_KEYS:
            assert packaged_default(key) is None, key
