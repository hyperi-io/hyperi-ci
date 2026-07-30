# Project:   HyperI CI
# File:      tests/unit/test_release_commit.py
# Purpose:   The release commit lands untagged, or not at all
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Committing rendered artefacts back must not recreate the #37 failure.

`@semantic-release/git` created the release tag on its own bot commit, so a
later history rewrite orphaned the tag and the next release recomputed a
version that already existed. The invariants that stop that recurring -- the
commit is never tagged, the ref is never force-updated -- are asserted here
rather than left to review.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci.release_commit import commit_release_artefacts


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "VERSION").write_text("3.1.0\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    return tmp_path


class _Api:
    """Records every gh api call and answers with a happy-path response."""

    def __init__(self, *, new_tree: str = "tree-new", ref_update: bool = True) -> None:
        self.calls: list[tuple[list[str], dict | None]] = []
        self.new_tree = new_tree
        self.ref_update = ref_update

    def __call__(self, args: list[str], *, body: dict | None = None) -> dict | None:
        self.calls.append((args, body))
        endpoint = args[-1]
        if endpoint.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "tip-sha"}}
        if "/git/commits/" in endpoint:
            return {"tree": {"sha": "tree-base"}}
        if endpoint.endswith("/git/blobs"):
            return {"sha": f"blob-{len(self.calls)}"}
        if endpoint.endswith("/git/trees"):
            return {"sha": self.new_tree}
        if endpoint.endswith("/git/commits"):
            return {"sha": "commit-new"}
        if "/git/refs/heads/" in endpoint:
            return {"ref": "refs/heads/main"} if self.ref_update else None
        return None

    def bodies_for(self, fragment: str) -> list[dict]:
        return [
            body
            for args, body in self.calls
            if body is not None and fragment in args[-1]
        ]

    def endpoints(self) -> list[str]:
        return [args[-1] for args, _ in self.calls]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/hyperi-ci")
    stub = _Api()
    with patch("hyperi_ci.release_commit._api", stub):
        yield stub


class TestTheInvariantsThatMatter:
    def test_never_creates_a_tag(self, api: _Api, project: Path) -> None:
        """The whole point: tags come from tag-head, never from here."""
        commit_release_artefacts(version="3.1.0", project_dir=project)
        assert not [e for e in api.endpoints() if "refs/tags" in e]

    def test_never_force_updates_the_ref(self, api: _Api, project: Path) -> None:
        """A force update would overwrite a concurrent push."""
        commit_release_artefacts(version="3.1.0", project_dir=project)
        for body in api.bodies_for("/git/refs/heads/"):
            assert body.get("force") is False

    def test_the_commit_skips_ci(self, api: _Api, project: Path) -> None:
        commit_release_artefacts(version="3.1.0", project_dir=project)
        message = api.bodies_for("/git/commits")[0]["message"]
        assert "[skip ci]" in message

    def test_the_commit_parent_is_the_branch_tip(
        self, api: _Api, project: Path
    ) -> None:
        commit_release_artefacts(version="3.1.0", project_dir=project)
        assert api.bodies_for("/git/commits")[0]["parents"] == ["tip-sha"]


class TestHappyPath:
    def test_returns_zero(self, api: _Api, project: Path) -> None:
        assert commit_release_artefacts(version="3.1.0", project_dir=project) == 0

    def test_commits_both_artefacts(self, api: _Api, project: Path) -> None:
        commit_release_artefacts(version="3.1.0", project_dir=project)
        paths = {e["path"] for e in api.bodies_for("/git/trees")[0]["tree"]}
        assert paths == {"VERSION", "CHANGELOG.md"}

    def test_builds_on_the_existing_tree(self, api: _Api, project: Path) -> None:
        """Without base_tree the commit would delete every other file."""
        commit_release_artefacts(version="3.1.0", project_dir=project)
        assert api.bodies_for("/git/trees")[0]["base_tree"] == "tree-base"

    def test_tolerates_a_leading_v(self, api: _Api, project: Path) -> None:
        commit_release_artefacts(version="v3.1.0", project_dir=project)
        assert "v3.1.0" in api.bodies_for("/git/commits")[0]["message"]

    def test_commits_only_the_artefact_that_exists(
        self, api: _Api, tmp_path: Path
    ) -> None:
        (tmp_path / "VERSION").write_text("3.1.0\n", encoding="utf-8")
        commit_release_artefacts(version="3.1.0", project_dir=tmp_path)
        paths = {e["path"] for e in api.bodies_for("/git/trees")[0]["tree"]}
        assert paths == {"VERSION"}


class TestNoOps:
    def test_an_identical_tree_creates_no_commit(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A re-run, or a release that changed neither file."""
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/hyperi-ci")
        stub = _Api(new_tree="tree-base")
        with patch("hyperi_ci.release_commit._api", stub):
            assert commit_release_artefacts(version="3.1.0", project_dir=project) == 0
        assert not stub.bodies_for("/git/commits")

    def test_no_artefacts_on_disk(self, api: _Api, tmp_path: Path) -> None:
        assert commit_release_artefacts(version="3.1.0", project_dir=tmp_path) == 0
        assert api.calls == []

    def test_dry_run_changes_nothing(self, api: _Api, project: Path) -> None:
        rc = commit_release_artefacts(
            version="3.1.0", project_dir=project, dry_run=True
        )
        assert rc == 0
        assert api.calls == []


class TestRefusals:
    def test_empty_version(self, api: _Api, project: Path) -> None:
        assert commit_release_artefacts(version="  ", project_dir=project) == 1

    def test_no_github_repository(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        assert commit_release_artefacts(version="3.1.0", project_dir=project) == 1

    def test_a_moving_branch_retries_then_fails(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected non-fast-forward means someone else pushed."""
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/hyperi-ci")
        stub = _Api(ref_update=False)
        with patch("hyperi_ci.release_commit._api", stub):
            assert commit_release_artefacts(version="3.1.0", project_dir=project) == 1
        assert len(stub.bodies_for("/git/refs/heads/")) == 3

    def test_an_unreadable_ref_fails_without_committing(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/hyperi-ci")
        with patch("hyperi_ci.release_commit._api", return_value=None) as stub:
            assert commit_release_artefacts(version="3.1.0", project_dir=project) == 1
        assert stub.call_count == 1
