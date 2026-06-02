# Project:   HyperI CI
# File:      tests/unit/test_recover_tags.py
# Purpose:   Tests for the issue #37 tag-recovery script
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tag-recovery (`scripts/recover-tags.py`) regression tests.

The script rebuilds release tags rewritten off-main by the #37 bug. The
load-bearing detail is the chore-commit regex: it must map a GA tag to its
GA `chore: version X.Y.Z` commit and NEVER to a `X.Y.Z-dev.N` prerelease
commit (an early bug did exactly that, mapping v1.14.1 -> 1.14.1-dev.1).
Uses a real git repo, no mocks.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "recover_tags",
    Path(__file__).resolve().parents[2] / "scripts" / "recover-tags.py",
)
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
    assert rt._CHORE_RE.match("fix: update hyperi-rustlib 1.20.0 to 1.20.1") is None
    assert rt._CHORE_RE.match("feat: add 1.2.3 support") is None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, subject: str) -> str:
    """Create an empty commit with `subject`, return its SHA."""
    _git(repo, "commit", "--allow-empty", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def test_release_commits_separates_ga_from_prerelease(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "feat: initial")
    dev = _commit(repo, "chore: version 1.0.0-dev.1 [skip ci]")
    ga = _commit(repo, "chore: version 1.0.0 [skip ci]")

    # no remotes -> falls back to scanning the named branch
    found = rt._release_commits(str(repo), "main")
    assert "1.0.0" in found
    assert ga in found["1.0.0"]
    assert dev not in found["1.0.0"]  # the bug: must not capture the prerelease
    assert "1.0.0-dev.1" not in found
