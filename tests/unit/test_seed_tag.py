# Project:   HyperI CI
# File:      tests/unit/test_seed_tag.py
# Purpose:   Seeding a repo's first version tag, against a real git repo
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""The seed tag is created once, from the manifest, and never a second time.

Real `git init` repos rather than mocks: the thing under test is what git ends
up holding, and a mocked `run_cmd` would prove only that the arguments looked
plausible.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyperi_ci.seed import existing_version_tags, seed_tag


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one commit and no tags."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "chore: initial")
    return tmp_path


class TestSeeding:
    def test_creates_the_declared_version(self, repo: Path) -> None:
        (repo / "Cargo.toml").write_text(
            '[package]\nname = "thing"\nversion = "1.4.2"\n', encoding="utf-8"
        )
        assert seed_tag(project_dir=repo) == 0
        assert existing_version_tags(repo) == ["v1.4.2"]

    def test_greenfield_starts_at_v0_1_0(self, repo: Path) -> None:
        assert seed_tag(project_dir=repo) == 0
        assert existing_version_tags(repo) == ["v0.1.0"]

    def test_tags_head(self, repo: Path) -> None:
        seed_tag(project_dir=repo)
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        tagged = _git(repo, "rev-parse", "v0.1.0^{commit}").stdout.strip()
        assert tagged == head

    def test_the_tag_says_it_is_not_a_release(self, repo: Path) -> None:
        """Tag-on-publish means a tag normally implies a published artefact."""
        seed_tag(project_dir=repo)
        message = _git(repo, "tag", "-l", "v0.1.0", "-n99").stdout
        assert "Not a published release" in message

    def test_ignores_a_stale_version_file(self, repo: Path) -> None:
        (repo / "VERSION").write_text("2.3.10\n", encoding="utf-8")
        seed_tag(project_dir=repo)
        assert existing_version_tags(repo) == ["v0.1.0"]


class TestIdempotence:
    def test_declines_when_a_tag_already_exists(self, repo: Path) -> None:
        _git(repo, "tag", "v7.0.0")
        assert seed_tag(project_dir=repo) == 0
        assert existing_version_tags(repo) == ["v7.0.0"]

    def test_running_twice_leaves_one_tag(self, repo: Path) -> None:
        seed_tag(project_dir=repo)
        assert seed_tag(project_dir=repo) == 0
        assert existing_version_tags(repo) == ["v0.1.0"]

    def test_a_non_version_tag_does_not_count(self, repo: Path) -> None:
        """`nightly` or `latest` is not a version — the repo still needs one."""
        _git(repo, "tag", "nightly")
        assert seed_tag(project_dir=repo) == 0
        assert existing_version_tags(repo) == ["v0.1.0"]


class TestRefusals:
    def test_dry_run_creates_nothing(self, repo: Path) -> None:
        assert seed_tag(project_dir=repo, dry_run=True) == 0
        assert existing_version_tags(repo) == []

    def test_not_a_git_repo(self, tmp_path: Path) -> None:
        assert seed_tag(project_dir=tmp_path) == 1

    def test_no_commits_yet(self, tmp_path: Path) -> None:
        _git(tmp_path, "init", "-q", "-b", "main")
        assert seed_tag(project_dir=tmp_path) == 1


class TestExistingVersionTags:
    def test_sorted_newest_first(self, repo: Path) -> None:
        for tag in ("v1.0.0", "v1.10.0", "v1.2.0"):
            _git(repo, "tag", tag)
        assert existing_version_tags(repo)[0] == "v1.10.0"

    def test_empty_outside_a_repo(self, tmp_path: Path) -> None:
        assert existing_version_tags(tmp_path) == []
