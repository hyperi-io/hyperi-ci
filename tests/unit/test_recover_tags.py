# Project:   HyperI CI
# File:      tests/unit/test_recover_tags.py
# Purpose:   Tests for the issue #37 tag-recovery script
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tag-recovery (`scripts/recover-tags.py`) regression tests.

The script rebuilds release tags rewritten off-main by the #37 bug. The
load-bearing detail is the chore-commit regex: it must map a GA tag to its
GA `chore: version X.Y.Z` commit and NEVER to a `X.Y.Z-dev.N` prerelease
commit (an early bug did exactly that, mapping v1.14.1 -> 1.14.1-dev.1).
Uses a real git repo, no mocks.
"""

import importlib.util
import sys
from pathlib import Path

from tests.git_helpers import commit, git, init_repo

_SPEC = importlib.util.spec_from_file_location(
    "recover_tags",
    Path(__file__).resolve().parents[2] / "scripts" / "recover-tags.py",
)
assert _SPEC is not None and _SPEC.loader is not None  # always resolves for a real file
rt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rt  # let @dataclass resolve Plan.__module__
_SPEC.loader.exec_module(rt)


def test_chore_re_matches_ga_forms():
    assert rt._CHORE_RE.match("chore: version 1.2.3 [skip ci]").group(1) == "1.2.3"
    assert rt._CHORE_RE.match("chore(release): 1.2.3 [skip ci]").group(1) == "1.2.3"
    assert rt._CHORE_RE.match("chore: version 10.20.30").group(1) == "10.20.30"


def test_chore_re_rejects_prerelease():
    # The #37-recovery bug: a GA tag must not match a -dev prerelease commit.
    assert rt._CHORE_RE.match("chore: version 1.14.1-dev.1 [skip ci]") is None
    assert rt._CHORE_RE.match("chore(release): 2.0.0-rc.1 [skip ci]") is None


def test_chore_re_ignores_unrelated_subjects():
    assert rt._CHORE_RE.match("fix: update scalo 1.20.0 to 1.20.1") is None
    assert rt._CHORE_RE.match("feat: add 1.2.3 support") is None


def test_release_commits_separates_ga_from_prerelease(tmp_path: Path):
    repo = init_repo(tmp_path / "r", branch="main")
    commit(repo, "feat: initial")
    dev = commit(repo, "chore: version 1.0.0-dev.1 [skip ci]")
    ga = commit(repo, "chore: version 1.0.0 [skip ci]")

    # no remotes -> falls back to scanning the named branch
    found = rt._release_commits(str(repo), "main")
    assert "1.0.0" in found
    assert ga in found["1.0.0"]
    assert dev not in found["1.0.0"]  # the bug: must not capture the prerelease
    assert "1.0.0-dev.1" not in found


def test_source_ref_restricts_to_that_line(tmp_path: Path):
    repo = init_repo(tmp_path / "r", branch="main")
    commit(repo, "feat: initial")
    commit(repo, "chore: version 1.0.0 [skip ci]")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "chore: version 2.0.0 [skip ci]")
    git(repo, "checkout", "-q", "main")

    # source_ref="main" must not see the version only on the side branch.
    on_main = rt._release_commits(str(repo), "main", source_ref="main")
    assert "1.0.0" in on_main
    assert "2.0.0" not in on_main
    # the side branch does carry it
    assert "2.0.0" in rt._release_commits(str(repo), "main", source_ref="side")
