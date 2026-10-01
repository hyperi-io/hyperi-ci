# Project:   HyperI CI
# File:      tests/unit/test_release_version.py
# Purpose:   Tests for the shared release-version resolver (HYPERCI_VERSION-first)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""`resolve_release_version` -- the single SSoT all stages use for the version
being released. HYPERCI_VERSION (Plan's next-version) wins over the committed
VERSION file, which is stale once stamping is central (#27 + zero-config)."""

import subprocess
from pathlib import Path

import pytest

from hyperi_ci import common
from hyperi_ci.common import (
    ReleaseVersionError,
    explicit_version,
    holds_latest,
    latest_version_tag,
    newer_release_than,
    resolve_release_version,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.io")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "commit", "--allow-empty", "-m", "chore: seed")
    return tmp_path


def test_hyperci_version_wins_and_strips_v(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HYPERCI_VERSION", "v1.2.3")
    (tmp_path / "VERSION").write_text("9.9.9\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_release_version() == "1.2.3"


def test_version_file_fallback_strips_v(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HYPERCI_VERSION", raising=False)
    (tmp_path / "VERSION").write_text("v4.5.6\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_release_version() == "4.5.6"


def test_none_when_neither_present(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HYPERCI_VERSION", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_release_version() is None


def test_empty_hyperci_version_ignored(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HYPERCI_VERSION", "   ")
    (tmp_path / "VERSION").write_text("7.0.0\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_release_version() == "7.0.0"


class TestTheVersionMustBeAVersion:
    """The value lands in image tags, labels and build args, after the logins."""

    @pytest.mark.parametrize(
        ("raw", "resolved"),
        [
            ("1.2.3", "1.2.3"),
            ("v1.2.3", "1.2.3"),
            ("1.2.0-beta.1", "1.2.0-beta.1"),
            ("1.2.3-rc.1+build.5", "1.2.3-rc.1+build.5"),
        ],
    )
    def test_semver_shapes_pass(
        self, monkeypatch, tmp_path: Path, raw: str, resolved: str
    ) -> None:
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        (tmp_path / "VERSION").write_text(f"{raw}\n")
        monkeypatch.chdir(tmp_path)
        assert resolve_release_version() == resolved

    @pytest.mark.parametrize(
        "raw", ["dev", "1.2", "1.2.3 x", '1.2.3"; curl x', "1.2.3-", "{}"]
    )
    def test_anything_else_in_the_file_is_refused(
        self, monkeypatch, tmp_path: Path, raw: str
    ) -> None:
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        (tmp_path / "VERSION").write_text(f"{raw}\n")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ReleaseVersionError, match="VERSION"):
            resolve_release_version()

    def test_anything_else_in_the_env_is_refused(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.3$(id)")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ReleaseVersionError, match="HYPERCI_VERSION"):
            resolve_release_version()

    def test_a_symlinked_version_file_is_refused(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Even one pointing at a valid version: the link itself is the attack."""
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.write_text("1.2.3\n")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "VERSION").symlink_to(elsewhere)
        monkeypatch.chdir(repo)
        with pytest.raises(ReleaseVersionError, match="symlink"):
            resolve_release_version()


class TestLatestVersionTag:
    """The last-resort fallback in `resolve_release_version` must never hand a
    build a prerelease as the version to stamp."""

    def test_release_tag_wins_over_a_higher_prerelease(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        _init_repo(tmp_path)
        _git(tmp_path, "tag", "v1.1.1")
        _git(tmp_path, "tag", "v1.1.2-beta.1")
        monkeypatch.chdir(tmp_path)
        assert latest_version_tag() == "1.1.1"

    def test_prerelease_only_repo_resolves_to_none(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        _init_repo(tmp_path)
        _git(tmp_path, "tag", "v1.1.2-beta.1")
        monkeypatch.chdir(tmp_path)
        assert latest_version_tag() is None


class TestNewerReleaseThan:
    """`latest` pointers stay on the newest stable release (PR #385 review).

    Re-publishing an older tag must publish its own artefacts without moving
    R2 `latest/`, GHCR `:latest` or the GitHub Release Latest flag.
    """

    @staticmethod
    def _tagged(tmp_path: Path, monkeypatch, *tags: str) -> None:
        _init_repo(tmp_path)
        for tag in tags:
            _git(tmp_path, "tag", tag)
        monkeypatch.chdir(tmp_path)

    def test_an_older_version_is_behind_the_newest_release(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        self._tagged(tmp_path, monkeypatch, "v1.0.4", "v1.0.5")
        assert newer_release_than("1.0.4") == "1.0.5"
        assert newer_release_than("v1.0.4") == "1.0.5"

    def test_the_newest_release_moves_latest(self, monkeypatch, tmp_path: Path) -> None:
        # A re-run of the current release, and a Tag & Release run after the
        # tag was cut, both see their own tag as the highest.
        self._tagged(tmp_path, monkeypatch, "v1.0.4", "v1.0.5")
        assert newer_release_than("1.0.5") is None

    def test_a_version_above_every_tag_moves_latest(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # The Container job runs before the new tag exists.
        self._tagged(tmp_path, monkeypatch, "v1.0.4", "v1.0.5")
        assert newer_release_than("1.0.6") is None

    def test_components_compare_as_numbers(self, monkeypatch, tmp_path: Path) -> None:
        self._tagged(tmp_path, monkeypatch, "v1.0.9", "v1.0.10")
        assert newer_release_than("1.0.9") == "1.0.10"
        assert newer_release_than("1.0.10") is None

    def test_a_prerelease_tag_never_counts_as_the_newest_release(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        self._tagged(tmp_path, monkeypatch, "v1.0.4", "v1.0.5-beta.1", "v2.0.0-rc.1")
        assert newer_release_than("1.0.4") is None

    def test_a_prerelease_version_is_never_compared(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        self._tagged(tmp_path, monkeypatch, "v1.0.5")
        assert newer_release_than("1.0.4-beta.1") is None

    def test_a_repo_with_no_tags_moves_latest(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        self._tagged(tmp_path, monkeypatch)
        assert newer_release_than("0.1.0") is None

    def test_holding_latest_says_so(self, monkeypatch, tmp_path: Path) -> None:
        self._tagged(tmp_path, monkeypatch, "v1.0.4", "v1.0.5")
        said: list[str] = []
        monkeypatch.setattr(common, "warn", said.append)

        assert holds_latest("1.0.4", "R2 latest/") is True
        assert said == [
            "Leaving R2 latest/ on v1.0.5: v1.0.4 is older than the newest "
            "stable release. Only its versioned artefacts publish."
        ]

    def test_the_newest_release_holds_nothing(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        self._tagged(tmp_path, monkeypatch, "v1.0.4", "v1.0.5")
        said: list[str] = []
        monkeypatch.setattr(common, "warn", said.append)

        assert holds_latest("1.0.5", "R2 latest/") is False
        assert said == []


class TestExplicitVersion:
    """`explicit_version` distinguishes a `--version X.Y.Z` override from a
    bump level travelling in the same `bump` channel (issue #37)."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1.18.4", "1.18.4"),
            ("v1.18.4", "1.18.4"),  # leading v tolerated + stripped
            ("  1.18.4  ", "1.18.4"),  # whitespace trimmed
            ("0.0.0", "0.0.0"),
            ("12.345.6789", "12.345.6789"),
        ],
    )
    def test_accepts_plain_semver(self, value: str, expected: str) -> None:
        assert explicit_version(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "auto",
            "patch",
            "minor",
            "1.2",  # too few components
            "1.2.3.4",  # too many
            "1.2.x",
            "1.2.3-rc1",  # no pre-release metadata
            "v",
            "latest",
        ],
    )
    def test_rejects_non_semver(self, value: str | None) -> None:
        assert explicit_version(value) is None
