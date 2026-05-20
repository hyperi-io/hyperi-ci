"""Tests for init_gitops.init_gitops()."""

from __future__ import annotations

import pytest

from hyperi_ci.init_gitops import GitopsInitError, init_gitops


def test_init_gitops_creates_directory_tree(tmp_path):
    target = tmp_path / "gitops"
    rc = init_gitops(target, org="hyperi-io")

    assert rc == 0
    assert (target / "README.md").exists()
    assert (target / "CODEOWNERS").exists()
    assert (target / "LICENSE").exists()
    assert (target / ".gitignore").exists()
    assert (target / ".gitbook.yaml").exists()
    assert (target / "mkdocs.yml").exists()
    assert (target / "topologies" / "README.md").exists()
    assert (target / "argocd" / "appprojects").is_dir()
    assert (target / ".github" / "workflows" / "validate.yaml").exists()
    assert (target / ".github" / "workflows" / "stitch-and-publish.yaml").exists()
    assert (target / ".github" / "workflows" / "docs.yaml").exists()
    assert (target / "docs" / "index.md").exists()
    assert (target / "docs" / "quickstart.md").exists()
    assert (target / "docs" / "concepts" / "overview.md").exists()


def test_init_gitops_substitutes_org_in_codeowners(tmp_path):
    target = tmp_path / "gitops"
    init_gitops(target, org="acme-corp")

    codeowners = (target / "CODEOWNERS").read_text(encoding="utf-8")
    assert "@acme-corp/platform" in codeowners
    assert "{{ ORG }}" not in codeowners


def test_init_gitops_rejects_non_empty_dir_without_force(tmp_path):
    target = tmp_path / "gitops"
    target.mkdir()
    (target / "junk.txt").write_text("hello")

    with pytest.raises(GitopsInitError) as exc_info:
        init_gitops(target, org="hyperi-io", force=False)

    assert "not empty" in str(exc_info.value).lower()


def test_init_gitops_overwrites_with_force(tmp_path):
    target = tmp_path / "gitops"
    target.mkdir()
    (target / "junk.txt").write_text("hello")

    rc = init_gitops(target, org="hyperi-io", force=True)

    assert rc == 0
    assert (target / "README.md").exists()
    # existing file is preserved — init_gitops does not delete user files
    assert (target / "junk.txt").exists()
    assert (target / "junk.txt").read_text() == "hello"
