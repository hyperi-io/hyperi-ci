# Project:   HyperI CI
# File:      tests/unit/test_release_commit.py
# Purpose:   The release commit lands untagged, or not at all
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Committing rendered artefacts back must not recreate the #37 failure.

`@semantic-release/git` created the release tag on its own bot commit, so a
later history rewrite orphaned the tag and the next release recomputed a
version that already existed. The invariants that stop that recurring -- the
commit is never tagged, the ref is never force-updated -- are asserted here
rather than left to review.
"""

from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import pytest
import yaml

from hyperi_ci import config as config_module
from hyperi_ci.common import run_cmd
from hyperi_ci.release_commit import (
    STAMP_OUTCOME_ENV,
    SUPPLEMENT,
    _local_blob,
    commit_release_artefacts,
)


@pytest.fixture(autouse=True)
def _restore_config_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """release-commit reloads config from tmp_path; keep that out of other tests."""
    monkeypatch.setattr(config_module, "_config_cache", None)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "VERSION").write_text("3.1.0\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [3.1.0](https://example.invalid/compare/v3.0.9...v3.1.0)"
        " (2026-09-16)\n",
        encoding="utf-8",
    )
    return tmp_path


_HEAD_BLOB = "head-blob-sha"


class _Api:
    """Records every gh api call and answers with a happy-path response."""

    def __init__(
        self,
        *,
        new_tree: str = "tree-new",
        ref_update: bool = True,
        supplement_on_branch: bool = True,
    ) -> None:
        self.calls: list[tuple[list[str], dict | None]] = []
        self.new_tree = new_tree
        self.ref_update = ref_update
        self.supplement_on_branch = supplement_on_branch
        # Blob shas of stamped files on the branch tip; a path not listed is
        # answered with the sha the checkout's HEAD carries.
        self.tip_blobs: dict[str, str | None] = {}

    def __call__(self, args: list[str], *, body: dict | None = None) -> dict | None:
        self.calls.append((args, body))
        endpoint = args[-1]
        if "/contents/" in endpoint:
            path = unquote(endpoint.split("/contents/", 1)[1].split("?", 1)[0])
            if path == SUPPLEMENT:
                return {"sha": "supplement-sha"} if self.supplement_on_branch else None
            sha = self.tip_blobs.get(path, _HEAD_BLOB)
            return {"sha": sha} if sha else None
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


class TestTheNotesSupplement:
    """One hand-written supplement reaches exactly one release."""

    @staticmethod
    def _write(project: Path) -> None:
        path = project / SUPPLEMENT
        path.parent.mkdir(parents=True)
        path.write_text("### Release notes\n\n- a thing\n", encoding="utf-8")

    def test_a_consumed_supplement_is_deleted(self, api: _Api, project: Path) -> None:
        self._write(project)
        commit_release_artefacts(version="3.1.0", project_dir=project)
        tree = api.bodies_for("/git/trees")[0]["tree"]
        removal = [entry for entry in tree if entry["path"] == SUPPLEMENT]
        assert removal == [
            {"path": SUPPLEMENT, "mode": "100644", "type": "blob", "sha": None}
        ]

    def test_without_one_the_tree_is_unchanged(self, api: _Api, project: Path) -> None:
        commit_release_artefacts(version="3.1.0", project_dir=project)
        tree = api.bodies_for("/git/trees")[0]["tree"]
        assert {entry["path"] for entry in tree} == {"VERSION", "CHANGELOG.md"}
        assert not [e for e in api.endpoints() if "/contents/" in e]

    def test_an_unrendered_release_keeps_it(self, api: _Api, project: Path) -> None:
        """A forced bump skips semantic-release, so the notes went nowhere."""
        self._write(project)
        (project / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        commit_release_artefacts(version="3.1.0", project_dir=project)
        tree = api.bodies_for("/git/trees")[0]["tree"]
        assert SUPPLEMENT not in {entry["path"] for entry in tree}

    def test_one_missing_from_the_branch_is_left_alone(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retroactive publish checks out a tag that still carries it."""
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/hyperi-ci")
        self._write(project)
        stub = _Api(supplement_on_branch=False)
        with patch("hyperi_ci.release_commit._api", stub):
            assert commit_release_artefacts(version="3.1.0", project_dir=project) == 0
        tree = stub.bodies_for("/git/trees")[0]["tree"]
        assert SUPPLEMENT not in {entry["path"] for entry in tree}


class TestStampPaths:
    """Files `release.stamp_cmd` wrote ride along with VERSION and the changelog."""

    @pytest.fixture(autouse=True)
    def _checkout_head(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
        """Blob shas in the checkout's HEAD; a path not listed carries _HEAD_BLOB."""
        blobs: dict[str, str | None] = {}
        monkeypatch.delenv(STAMP_OUTCOME_ENV, raising=False)
        monkeypatch.setattr(
            "hyperi_ci.release_commit._local_blob",
            lambda _root, name: blobs.get(name, _HEAD_BLOB),
        )
        return blobs

    @staticmethod
    def _configure(project: Path, paths: object) -> None:
        (project / ".hyperi-ci.yaml").write_text(
            yaml.safe_dump({"release": {"stamp_paths": paths}}), encoding="utf-8"
        )

    @staticmethod
    def _spec(project: Path, name: str) -> None:
        path = project / "openapi-spec" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('{"info": {"version": "3.1.0"}}\n', encoding="utf-8")

    def _tree_paths(self, api: _Api) -> set[str]:
        return {entry["path"] for entry in api.bodies_for("/git/trees")[0]["tree"]}

    def test_listed_files_join_the_commit(self, api: _Api, project: Path) -> None:
        self._spec(project, "openapi.json")
        self._spec(project, "openapi.e2e.json")
        self._configure(
            project, ["openapi-spec/openapi.json", "openapi-spec/openapi.e2e.json"]
        )
        assert commit_release_artefacts(version="3.1.0", project_dir=project) == 0
        assert self._tree_paths(api) == {
            "VERSION",
            "CHANGELOG.md",
            "openapi-spec/openapi.json",
            "openapi-spec/openapi.e2e.json",
        }

    def test_a_listed_file_not_on_disk_is_skipped(
        self, api: _Api, project: Path
    ) -> None:
        self._spec(project, "openapi.json")
        self._configure(
            project, ["openapi-spec/openapi.json", "openapi-spec/gone.json"]
        )
        assert commit_release_artefacts(version="3.1.0", project_dir=project) == 0
        assert self._tree_paths(api) == {
            "VERSION",
            "CHANGELOG.md",
            "openapi-spec/openapi.json",
        }

    def test_a_path_out_of_the_repo_still_commits_the_rest(
        self, api: _Api, project: Path
    ) -> None:
        """A bad entry costs the extras, never VERSION and the changelog."""
        self._configure(project, ["../../etc/passwd"])
        assert commit_release_artefacts(version="3.1.0", project_dir=project) == 0
        assert self._tree_paths(api) == {"VERSION", "CHANGELOG.md"}

    def test_a_fixed_artefact_listed_again_is_not_doubled(
        self, api: _Api, project: Path
    ) -> None:
        self._configure(project, ["VERSION"])
        commit_release_artefacts(version="3.1.0", project_dir=project)
        tree = api.bodies_for("/git/trees")[0]["tree"]
        assert [entry["path"] for entry in tree].count("VERSION") == 1

    def test_a_file_the_branch_changed_since_the_checkout_is_left_alone(
        self, api: _Api, project: Path
    ) -> None:
        """A merge during the release regenerated it from newer source."""
        self._spec(project, "openapi.json")
        self._spec(project, "openapi.e2e.json")
        self._configure(
            project, ["openapi-spec/openapi.json", "openapi-spec/openapi.e2e.json"]
        )
        api.tip_blobs["openapi-spec/openapi.json"] = "newer-on-main"
        assert commit_release_artefacts(version="3.1.0", project_dir=project) == 0
        assert self._tree_paths(api) == {
            "VERSION",
            "CHANGELOG.md",
            "openapi-spec/openapi.e2e.json",
        }

    def test_a_file_new_to_both_sides_is_committed(
        self, api: _Api, project: Path, _checkout_head: dict[str, str | None]
    ) -> None:
        self._spec(project, "openapi.json")
        self._configure(project, ["openapi-spec/openapi.json"])
        _checkout_head["openapi-spec/openapi.json"] = None
        api.tip_blobs["openapi-spec/openapi.json"] = None
        commit_release_artefacts(version="3.1.0", project_dir=project)
        assert "openapi-spec/openapi.json" in self._tree_paths(api)

    def test_a_file_added_on_the_branch_since_the_checkout_is_left_alone(
        self, api: _Api, project: Path, _checkout_head: dict[str, str | None]
    ) -> None:
        self._spec(project, "openapi.json")
        self._configure(project, ["openapi-spec/openapi.json"])
        _checkout_head["openapi-spec/openapi.json"] = None
        commit_release_artefacts(version="3.1.0", project_dir=project)
        assert "openapi-spec/openapi.json" not in self._tree_paths(api)

    def test_a_failed_stamp_step_keeps_its_output_out(
        self, api: _Api, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A generator that died part-way leaves partial files behind."""
        self._spec(project, "openapi.json")
        self._configure(project, ["openapi-spec/openapi.json"])
        monkeypatch.setenv(STAMP_OUTCOME_ENV, "failure")
        assert commit_release_artefacts(version="3.1.0", project_dir=project) == 0
        assert self._tree_paths(api) == {"VERSION", "CHANGELOG.md"}

    def test_a_successful_stamp_step_commits_its_output(
        self, api: _Api, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._spec(project, "openapi.json")
        self._configure(project, ["openapi-spec/openapi.json"])
        monkeypatch.setenv(STAMP_OUTCOME_ENV, "success")
        commit_release_artefacts(version="3.1.0", project_dir=project)
        assert "openapi-spec/openapi.json" in self._tree_paths(api)

    @pytest.mark.parametrize("name", [".git/config", SUPPLEMENT])
    def test_paths_release_commit_owns_are_refused(
        self, api: _Api, project: Path, name: str
    ) -> None:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
        self._configure(project, [name])
        commit_release_artefacts(version="3.1.0", project_dir=project)
        blobs = [e for e in api.bodies_for("/git/trees")[0]["tree"] if e["sha"]]
        assert name not in {entry["path"] for entry in blobs}

    def test_a_symlink_is_not_flattened_into_a_file(
        self, api: _Api, project: Path
    ) -> None:
        self._spec(project, "openapi.json")
        (project / "openapi-spec" / "alias.json").symlink_to("openapi.json")
        self._configure(project, ["openapi-spec/alias.json"])
        commit_release_artefacts(version="3.1.0", project_dir=project)
        assert "openapi-spec/alias.json" not in self._tree_paths(api)

    def test_an_executable_keeps_its_mode(self, api: _Api, project: Path) -> None:
        script = project / "bin" / "tool"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)
        self._configure(project, ["bin/tool"])
        commit_release_artefacts(version="3.1.0", project_dir=project)
        modes = {e["path"]: e["mode"] for e in api.bodies_for("/git/trees")[0]["tree"]}
        assert modes["bin/tool"] == "100755"
        assert modes["VERSION"] == "100644"


class TestLocalBlob:
    """The checkout's HEAD blob, compared against the branch tip."""

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
        result = run_cmd(["git", "-C", str(root), *identity, *args], capture=True)
        return result.stdout.strip()

    def test_matches_git_hash_object(self, tmp_path: Path) -> None:
        self._git(tmp_path, "init", "-q")
        (tmp_path / "spec.json").write_text("{}\n", encoding="utf-8")
        self._git(tmp_path, "add", "spec.json")
        self._git(tmp_path, "commit", "-q", "-m", "x")
        expected = self._git(tmp_path, "hash-object", "spec.json")
        assert _local_blob(tmp_path, "spec.json") == expected

    def test_a_path_head_lacks_is_none(self, tmp_path: Path) -> None:
        self._git(tmp_path, "init", "-q")
        (tmp_path / "a").write_text("a\n", encoding="utf-8")
        self._git(tmp_path, "add", "a")
        self._git(tmp_path, "commit", "-q", "-m", "x")
        assert _local_blob(tmp_path, "spec.json") is None


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
