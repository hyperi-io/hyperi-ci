# Project:   HyperI CI
# File:      tests/unit/test_spill.py
# Purpose:   Tests for `hyperi-ci spill` -- classify, scan, and fix.
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the spill scanner + remediator.

Real throwaway git repos (no mocks): git init + commits + a bare "origin" so
the pushed/unpushed/untracked classification is exercised for real, exactly as
it runs in the field.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyperi_ci import spill

# ---------------------------------------------------------------------------
# git helpers -- real repos
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.stdout.strip()


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")


def _commit(repo: Path, rel: str, body: str = "x") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "-m", f"add {rel}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    _init(r)
    _commit(r, "src/app.py", "print('hi')\n")  # an innocuous baseline commit
    return r


@pytest.fixture
def repo_with_origin(tmp_path: Path) -> Path:
    """A repo whose main tracks a bare 'origin' -- pushed history exists."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    r = tmp_path / "repo"
    _init(r)
    _commit(r, "src/app.py", "print('hi')\n")
    _git(r, "remote", "add", "origin", str(origin))
    _git(r, "push", "-q", "-u", "origin", "main")
    return r


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "category", "severity"),
    [
        ("CLAUDE.md", "ai-artefact", "high"),
        (".claude/settings.json", "ai-artefact", "high"),
        ("AGENTS.md", "ai-artefact", "high"),
        (".hyperi-ai/plans/x.md", "ai-artefact", "high"),
        (".env", "secret", "critical"),
        ("deploy/id_rsa", "secret", "critical"),
        ("secrets/prod.json", "secret", "critical"),
        ("app/db-credentials.yaml", "secret", "critical"),
        ("notes/.DS_Store", "vcs-cruft", "low"),
        ("dist/bundle.js", "build-artefact", "medium"),
        ("run.log", "log-dump", "low"),
    ],
)
def test_classify_hits(path: str, category: str, severity: str) -> None:
    cat = spill.classify(path)
    assert cat is not None, path
    assert (cat.name, cat.severity) == (category, severity)


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",                 # ordinary source
        "config/.env.example",        # a template, not a secret
        "src/secrets_manager.py",     # code that HANDLES secrets, not a secret
        "README.md",
    ],
)
def test_classify_misses(path: str) -> None:
    assert spill.classify(path) is None


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def test_scan_clean_repo(repo: Path) -> None:
    report = spill.scan(repo)
    assert report.findings == []
    assert report.remediation() == "none"
    assert report.worst == "none"


def test_scan_finds_ai_artefact_unpushed(repo: Path) -> None:
    _commit(repo, "CLAUDE.md", "notes\n")
    report = spill.scan(repo, rev_range="HEAD")
    hits = [f for f in report.findings if f.path == "CLAUDE.md"]
    assert len(hits) == 1
    f = hits[0]
    assert f.category == "ai-artefact" and f.where == "unpushed"


def test_scan_distinguishes_pushed_from_unpushed(repo_with_origin: Path) -> None:
    _commit(repo_with_origin, ".env", "SECRET=1\n")          # committed + pushed
    _git(repo_with_origin, "push", "-q", "origin", "main")
    _commit(repo_with_origin, "CLAUDE.md", "notes\n")        # committed, NOT pushed
    report = spill.scan(repo_with_origin, rev_range="HEAD")
    where = {f.path: f.where for f in report.findings}
    assert where.get(".env") == "pushed"
    assert where.get("CLAUDE.md") == "unpushed"
    # A pushed secret is the worst case -> history rewrite.
    assert report.worst == "critical"
    assert report.remediation() == "history-rewrite"


def test_scan_default_range_is_unpushed_only(repo_with_origin: Path) -> None:
    # A spill already in the pushed history is NOT in @{u}..HEAD.
    _commit(repo_with_origin, ".env", "SECRET=1\n")
    _git(repo_with_origin, "push", "-q", "origin", "main")
    report = spill.scan(repo_with_origin)  # default range
    assert [f.path for f in report.findings] == []


def test_scan_finds_staged_untracked(repo: Path) -> None:
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    _git(repo, "add", ".env")  # staged, not committed
    report = spill.scan(repo)
    hits = [f for f in report.findings if f.path == ".env"]
    assert hits and hits[0].where == "untracked"
    assert report.remediation() == "untrack"


def test_scan_flags_gitignored_but_committed(repo: Path) -> None:
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "gitignore")
    _commit(repo, "build/out.js", "x\n")  # force-added past the ignore
    report = spill.scan(repo, rev_range="HEAD")
    hits = [f for f in report.findings if f.path == "build/out.js"]
    assert hits and hits[0].gitignored is True


def test_exposure_reports_upstream(repo_with_origin: Path) -> None:
    ex = spill.assess_exposure(repo_with_origin)
    assert ex.has_upstream is True
    assert ex.remote_url.endswith("origin.git")


def test_report_to_dict_shape(repo: Path) -> None:
    _commit(repo, ".env", "S=1\n")
    d = spill.scan(repo, rev_range="HEAD").to_dict()
    assert d["remediation"] in {"untrack", "amend-or-rebase", "history-rewrite"}
    assert d["worst_severity"] == "critical"
    assert any(f["path"] == ".env" for f in d["findings"])


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------


def test_fix_dry_run_changes_nothing(repo: Path) -> None:
    _commit(repo, "CLAUDE.md", "notes\n")
    before = _git(repo, "status", "--short")
    assert spill.fix(repo, ["CLAUDE.md"], mode="untrack", execute=False) == 0
    assert _git(repo, "status", "--short") == before  # untouched
    assert (repo / "CLAUDE.md").is_file()


def test_fix_untrack_execute(repo: Path) -> None:
    _commit(repo, "CLAUDE.md", "notes\n")
    assert spill.fix(repo, ["CLAUDE.md"], mode="untrack", execute=True) == 0
    tracked = _git(repo, "ls-files", "CLAUDE.md")
    assert tracked == ""  # no longer tracked (staged removal)
    assert "CLAUDE.md" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_fix_amend_execute(repo: Path) -> None:
    _commit(repo, "CLAUDE.md", "notes\n")
    head_before = _git(repo, "rev-parse", "HEAD")
    assert spill.fix(repo, ["CLAUDE.md"], mode="amend", execute=True) == 0
    assert _git(repo, "rev-parse", "HEAD") != head_before  # commit rewritten
    assert _git(repo, "ls-files", "CLAUDE.md") == ""


def test_fix_rewrite_dry_run_is_safe(repo: Path) -> None:
    _commit(repo, ".env", "S=1\n")
    head_before = _git(repo, "rev-parse", "HEAD")
    assert spill.fix(repo, [".env"], mode="rewrite", execute=False) == 0
    assert _git(repo, "rev-parse", "HEAD") == head_before  # nothing rewritten


def test_fix_rejects_unknown_mode(repo: Path) -> None:
    assert spill.fix(repo, ["x"], mode="bogus", execute=True) == 2


def test_fix_rejects_empty_paths(repo: Path) -> None:
    assert spill.fix(repo, [], mode="untrack", execute=True) == 2


# ---------------------------------------------------------------------------
# AI-tool file coverage (claude, cursor, codex, gemini, copilot, aider, ...)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "GEMINI.md",
        ".cursor/rules/x.mdc",
        ".codex/config.toml",
        ".github/copilot-instructions.md",
        ".aider.conf.yml",
        ".windsurfrules",
        ".codeium/x",
        ".clinerules",
        "sub/dir/CLAUDE.md",
    ],
)
def test_classify_ai_tool_files(path: str) -> None:
    cat = spill.classify(path)
    assert cat is not None and cat.name == "ai-artefact", path


@pytest.mark.parametrize(
    "path",
    [
        "src/agents.py",          # not AGENTS.md
        "docs/claude-usage.md",   # mentions claude, but not a CLAUDE.md artefact
        "cursorrules.py",         # not .cursorrules
    ],
)
def test_classify_ai_tool_false_positives(path: str) -> None:
    assert spill.classify(path) is None


# ---------------------------------------------------------------------------
# AI attribution -- commit messages/authors + file content
# ---------------------------------------------------------------------------


def _commit_msg(repo: Path, rel: str, msg: str, body: str = "x") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "-m", msg)


def test_commit_attribution_trailer_detected(repo: Path) -> None:
    _commit_msg(
        repo, "feature.py",
        "feat: add thing\n\nCo-authored-by: Claude <noreply@anthropic.com>",
        "print('x')\n",
    )
    hits = spill.commit_attributions(repo, "HEAD")
    assert any("feat: add thing" in subj for _sha, subj, _where in hits)
    report = spill.scan(repo, rev_range="HEAD")
    assert any(f.category == "ai-attribution-commit" for f in report.findings)


def test_clean_commit_no_attribution(repo: Path) -> None:
    _commit_msg(repo, "feature.py", "fix: a normal human commit", "print('x')\n")
    assert spill.commit_attributions(repo, "HEAD") == []


def test_content_attribution_detected(repo: Path) -> None:
    _commit(repo, "util.py", "# Generated with Claude Code\ndef f(): ...\n")
    hits = spill.content_attributions(repo, "HEAD")
    assert any(file == "util.py" for file, _snip in hits)
    report = spill.scan(repo, rev_range="HEAD")
    assert any(f.category == "ai-attribution" for f in report.findings)


def test_content_no_false_positive_on_plain_code(repo: Path) -> None:
    _commit(repo, "plain.py", "def add(a, b):\n    return a + b\n")
    assert spill.content_attributions(repo, "HEAD") == []
