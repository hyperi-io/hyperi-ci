# Project:   HyperI CI
# File:      src/hyperi_ci/spill.py
# Purpose:   Find + remediate files that leaked into git commits ("spills").
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Find and remediate files that leaked into git commits ("spills").

Agents (and humans) routinely commit files they should NOT: AI-assistant
artefacts (CLAUDE.md, .claude/, plans, handovers), secrets (.env, keys),
scratch, and build output. This module powers two commands:

- ``hyperi-ci spill`` (scan, READ-ONLY, always safe): walks a commit range,
  classifies each added path by category + severity, works out the EXPOSURE
  (repo public/private, pushed vs local-only), and recommends a remediation
  path. ``--json`` emits the machine form the `/spill` agent skill consumes.
- ``hyperi-ci spill fix`` (remediate, GUARDED): untracks / amends for the
  safe cases; for already-pushed leaks it prepares a filtered rewrite on a
  BACKUP branch and stops -- the final force-push of a protected branch is a
  shared-history rewrite that a human performs, never the agent (the hyperi-ai
  danger guard keeps force-push-to-main tier-0 / non-grantable by design).

Rewriting history does NOT un-leak a secret: anyone who fetched still has it.
Every secret finding carries a ROTATE-THE-SECRET instruction, loud.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn

# ---------------------------------------------------------------------------
# Classification -- what a spilled path looks like, by SHAPE not a fixed list
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(frozen=True)
class Category:
    """A class of file that usually should not be committed."""

    name: str
    severity: str  # critical | high | medium | low
    why: str
    remove_default: bool  # removed unless the user explicitly keeps it
    pattern: re.Pattern[str]


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


# NB: a .env TEMPLATE (.env.example/sample/template/dist) is committed
# documentation, not a secret -- excluded from the secret pattern.
_CATEGORIES: tuple[Category, ...] = (
    Category(
        "secret", "critical",
        "credential/secret material -- a leak, not a mistake. ROTATE it: a "
        "history rewrite does NOT un-leak what was already fetched.",
        True,
        _rx(
            r"(?:^|/)\.env(?!\.(?:example|sample|template|dist|defaults)(?:\.|$))(?:\.[^/]*)?$"
            r"|\.(?:pem|key|p12|pfx|jks|keystore)$"
            r"|(?:^|/)credentials(?:\.[^/]*)?$"
            r"|(?:^|/)[^/]*(?:secret|credential)[^/]*\.(?:env|json|ya?ml|toml|ini|cfg|conf|txt)$"
            r"|(?:^|/)secrets?/"
            r"|(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)\b"
            r"|(?:^|/)\.(?:ssh|aws|gnupg)/"
        ),
    ),
    Category(
        "ai-artefact", "high",
        "AI-assistant footprint -- house rule is ZERO committed AI artefacts "
        "(agent memory/config/plans/handovers for claude, cursor, codex, "
        "gemini, copilot, aider, windsurf...).",
        True,
        _rx(
            # Agent memory / instruction files (anchored to the whole basename).
            r"(?:^|/)(?:CLAUDE|AGENTS?|GEMINI|GPT|COPILOT|CONVENTIONS)\.md$"
            r"|(?:^|/)TODO\.md$"
            r"|(?:^|/)\.github/copilot-instructions\.md$"
            # Per-tool config DIRS.
            r"|(?:^|/)\.(?:claude|cursor|codex|gemini|aider|windsurf|codeium"
            r"|continue|github-copilot|goose|cline|roo)(?:/|$)"
            # Per-tool dotfiles (whole basename).
            r"|(?:^|/)\.(?:cursorrules|cursorignore|codeiumignore|windsurfrules"
            r"|aiderignore|clinerules|mcp\.json)$"
            r"|(?:^|/)\.aider(?:\.[^/]*)?$"          # .aider.conf.yml, .aider.chat.history.md
            # hyperi-ai + superpowers artefacts.
            r"|(?:^|/)\.hyperi-ai(?:/|$)"
            r"|(?:^|/)docs/superpowers/"
            r"|(?:^|/)(?:handover|HANDOVER)\.md$"
        ),
    ),
    Category(
        "vcs-cruft", "low",
        "editor/OS/merge cruft -- never belongs in a tree.",
        True,
        _rx(
            r"(?:^|/)\.DS_Store$|(?:^|/)Thumbs\.db$"
            r"|[._][^/]*\.sw[a-p]$|~$|\.bak$|\.orig$|\.rej$"
        ),
    ),
    Category(
        "build-artefact", "medium",
        "build output / dependency dir -- rebuildable, bloats history.",
        True,
        _rx(
            r"(?:^|/)(?:dist|build|target|node_modules|__pycache__|\.venv"
            r"|coverage|htmlcov)(?:/|$)"
            r"|\.pyc$|\.egg-info(?:/|$)"
        ),
    ),
    Category(
        "log-dump", "low",
        "log / data dump -- usually accidental, sometimes large.",
        True,
        _rx(r"\.log$|(?:^|/)core\.\d+$|(?:^|/)npm-debug\.log"),
    ),
)


def classify(path: str) -> Category | None:
    """Return the highest-severity category matching a path, or None."""
    hits = [c for c in _CATEGORIES if c.pattern.search(path)]
    if not hits:
        return None
    return min(hits, key=lambda c: SEVERITY_ORDER[c.severity])


# ---------------------------------------------------------------------------
# AI attribution -- the agents' habit of crediting themselves everywhere.
# HIGH-PRECISION MARKERS ONLY: explicit self-attribution left in commit
# trailers, author identities, and file content. Deliberately NOT a "reads
# like AI" heuristic -- that is where false positives live (and the whole
# point is to be trustworthy, not eager).
# ---------------------------------------------------------------------------

_AI_ATTRIBUTION_RE = re.compile(
    # Commit trailer: Co-authored-by a known agent / bot.
    r"co-authored-by:[^\n]*"
    r"(?:claude|cursor|copilot|gemini|codex|chatgpt|openai|devin|aider"
    r"|windsurf|codeium|tabnine|sourcegraph|cody|\[bot\])"
    # "Generated/written/assisted with|by <agent>".
    r"|(?:generated|written|authored|created|assisted)\s+(?:with|by)\s+"
    r"(?:claude|cursor|copilot|gemini|codex|chatgpt|openai|github\s+copilot)"
    # The Claude Code / robot-emoji footer.
    r"|generated with \[?claude code"
    r"|\U0001f916\s*generated with"
    # Agent no-reply / service emails.
    r"|noreply@anthropic\.com"
    r"|@(?:anthropic|openai|cursor|codeium)\.com",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Git helpers (read-only)
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, check: bool = False) -> str:
    """Run a git command, return stripped stdout ('' on failure)."""
    r = run_cmd(["git", *args], check=check, capture=True, cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def _upstream(cwd: Path) -> str | None:
    """The tracking ref (e.g. origin/main), or None if none is set."""
    ref = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd)
    return ref or None


def default_range(cwd: Path) -> str:
    """Commits to scan by default: the unpushed ones (@{upstream}..HEAD),
    else the whole of HEAD when there is no upstream (nothing is pushed yet)."""
    up = _upstream(cwd)
    return f"{up}..HEAD" if up else "HEAD"


def _added_paths(rev_range: str, cwd: Path) -> dict[str, list[str]]:
    """Map each path ADDED in the range to the commits that touched it.

    Uses --diff-filter=A on name-only log so a path that was later removed in
    the same range is still surfaced (it was still committed + pushed-able).
    """
    out = _git(
        ["log", "--no-merges", "--diff-filter=AM", "--name-only",
         "--pretty=format:commit %h", rev_range],
        cwd,
    )
    result: dict[str, list[str]] = {}
    current = ""
    for line in out.splitlines():
        if line.startswith("commit "):
            current = line.split(" ", 1)[1].strip()
        elif line.strip():
            result.setdefault(line.strip(), []).append(current)
    return result


def _in_upstream(path: str, cwd: Path) -> bool:
    """True if the path exists anywhere in the pushed (upstream) history."""
    up = _upstream(cwd)
    if not up:
        return False
    return bool(_git(["log", "-1", "--oneline", up, "--", path], cwd))


def _gitignored(paths: list[str], cwd: Path) -> set[str]:
    """Subset of paths the repo's own .gitignore matches (force-added)."""
    if not paths:
        return set()
    # Probe individually (small candidate set). --no-index is essential: a
    # force-added path is already TRACKED, and plain check-ignore reports only
    # UNtracked matches -- the spill we care about (committed past .gitignore)
    # is exactly a tracked path that still matches an ignore rule.
    hit: set[str] = set()
    for p in paths:
        probe = run_cmd(
            ["git", "check-ignore", "--no-index", p], check=False, capture=True, cwd=cwd
        )
        if probe.returncode == 0:
            hit.add(p)
    return hit


def _commit_in_upstream(sha: str, cwd: Path) -> bool:
    """True if the commit is an ancestor of the pushed (upstream) tip."""
    up = _upstream(cwd)
    if not up:
        return False
    r = run_cmd(
        ["git", "merge-base", "--is-ancestor", sha, up], check=False, capture=True, cwd=cwd
    )
    return r.returncode == 0


def commit_attributions(cwd: Path, rev_range: str) -> list[tuple[str, str, str]]:
    """Commits in the range whose message OR author carries an AI attribution.

    Returns ``(short_sha, subject, where)``. Catches the Co-authored-by-Claude
    trailer, the robot-emoji footer, and an agent no-reply author/committer.
    """
    shas = [
        s for s in _git(["log", "--no-merges", "--format=%H", rev_range], cwd).splitlines()
        if s.strip()
    ]
    hits: list[tuple[str, str, str]] = []
    for sha in shas:
        # author name/email + committer name/email + subject + body.
        blob = _git(["show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%s%n%b", sha], cwd)
        if _AI_ATTRIBUTION_RE.search(blob):
            lines = blob.splitlines()
            subject = lines[4] if len(lines) > 4 else ""
            where = "pushed" if _commit_in_upstream(sha, cwd) else "unpushed"
            hits.append((sha[:9], subject, where))
    return hits


def content_attributions(cwd: Path, rev_range: str) -> list[tuple[str, str]]:
    """Files with an ADDED line carrying an AI attribution in the range.

    Returns ``(file, snippet)``. Scans the added (`+`) lines of the range's
    diff, so an attribution smuggled into a code comment or doc is caught, not
    just whole-file artefacts.
    """
    out = _git(
        ["log", rev_range, "--no-merges", "-p", "--unified=0", "--format=%H"], cwd
    )
    hits: list[tuple[str, str]] = []
    current = ""
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif (
            line.startswith("+")
            and not line.startswith("+++")
            and _AI_ATTRIBUTION_RE.search(line)
        ):
            hits.append((current, line[1:].strip()[:100]))
    return hits


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


@dataclass
class Exposure:
    visibility: str  # public | private | unknown
    has_upstream: bool
    remote_url: str


def assess_exposure(cwd: Path) -> Exposure:
    """Repo visibility (via gh, best-effort) + whether it has a push remote."""
    url = _git(["remote", "get-url", "origin"], cwd)
    visibility = "unknown"
    gh = run_cmd(
        ["gh", "repo", "view", "--json", "visibility", "-q", ".visibility"],
        check=False, capture=True, cwd=cwd,
    )
    if gh.returncode == 0 and gh.stdout.strip():
        visibility = gh.stdout.strip().lower()
    return Exposure(visibility=visibility, has_upstream=_upstream(cwd) is not None,
                    remote_url=url)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    path: str
    category: str
    severity: str
    why: str
    remove_default: bool
    commits: list[str]
    where: str  # pushed | unpushed | untracked
    gitignored: bool


@dataclass
class Report:
    rev_range: str
    exposure: Exposure
    findings: list[Finding] = field(default_factory=list)

    @property
    def worst(self) -> str:
        if not self.findings:
            return "none"
        return min((f.severity for f in self.findings), key=lambda s: SEVERITY_ORDER[s])

    def remediation(self) -> str:
        """The single recommended remediation path for the whole report."""
        if not self.findings:
            return "none"
        if any(f.where == "pushed" for f in self.findings):
            return "history-rewrite"
        if any(f.where == "unpushed" for f in self.findings):
            return "amend-or-rebase"
        return "untrack"

    def to_dict(self) -> dict:
        return {
            "rev_range": self.rev_range,
            "exposure": vars(self.exposure),
            "worst_severity": self.worst,
            "remediation": self.remediation(),
            "findings": [vars(f) for f in self.findings],
        }


def scan(cwd: Path, rev_range: str | None = None, include_untracked: bool = True) -> Report:
    """Build a spill Report for a commit range (default: unpushed commits)."""
    rng = rev_range or default_range(cwd)
    exposure = assess_exposure(cwd)
    report = Report(rev_range=rng, exposure=exposure)

    added = _added_paths(rng, cwd)
    # Staged-but-uncommitted additions count too (a spill you can catch before
    # it is even committed -- the cheapest fix).
    staged: dict[str, list[str]] = {}
    if include_untracked:
        for p in _git(["diff", "--cached", "--name-only", "--diff-filter=A"], cwd).splitlines():
            if p.strip():
                staged.setdefault(p.strip(), [])

    candidates = {**added, **{k: v for k, v in staged.items() if k not in added}}
    ignored = _gitignored(list(candidates), cwd)

    for path, commits in sorted(candidates.items()):
        cat = classify(path)
        is_ignored = path in ignored
        if cat is None and not is_ignored:
            continue  # not a recognised spill shape and not self-gitignored
        if cat is None:
            # gitignored-but-committed with no category -> flag as medium cruft.
            cat = Category("gitignored", "medium",
                           "matches the repo's own .gitignore but was committed "
                           "(force-added) -- almost always unintended.", True,
                           re.compile(""))
        where = (
            "untracked" if not commits else
            "pushed" if _in_upstream(path, cwd) else
            "unpushed"
        )
        report.findings.append(Finding(
            path=path, category=cat.name, severity=cat.severity, why=cat.why,
            remove_default=cat.remove_default, commits=[c for c in commits if c],
            where=where, gitignored=is_ignored,
        ))
    # AI self-attribution -- in commit messages/authors, and in added content.
    # These are not file spills: a commit trailer is fixed by rewording/amending
    # (a pushed one needs a history rewrite), a content line by editing it out.
    for sha, subject, where in commit_attributions(cwd, rng):
        report.findings.append(Finding(
            path=f"commit {sha}: {subject}"[:100],
            category="ai-attribution-commit", severity="high",
            why="AI self-attribution in a commit (trailer/author). House rule "
                "bans AI attribution -- reword/amend it out (a pushed one needs "
                "a history rewrite).",
            remove_default=True, commits=[sha], where=where, gitignored=False,
        ))
    seen: set[str] = set()
    for file, snippet in content_attributions(cwd, rng):
        key = f"{file}\x00{snippet}"
        if key in seen:
            continue
        seen.add(key)
        report.findings.append(Finding(
            path=file or "(diff)", category="ai-attribution", severity="high",
            why=f"AI self-attribution in file content ({snippet!r}). House rule "
                "bans AI attribution -- edit the line out.",
            remove_default=True, commits=[],
            where="pushed" if file and _in_upstream(file, cwd) else "unpushed",
            gitignored=False,
        ))

    report.findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_SEV_LABEL = {"critical": "CRITICAL", "high": "HIGH", "medium": "medium", "low": "low"}


def render(report: Report) -> None:
    """Print a human-readable spill report."""
    ex = report.exposure
    info(f"spill scan: {report.rev_range}")
    info(
        f"  exposure: repo is {ex.visibility.upper()}, "
        + ("has a push remote" if ex.has_upstream else "no push remote (local-only)")
    )
    if not report.findings:
        success("  no spills found -- nothing committed that looks out of place.")
        return
    for f in report.findings:
        tag = _SEV_LABEL[f.severity]
        loc = {"pushed": "PUSHED", "unpushed": "unpushed local", "untracked": "staged"}[f.where]
        line = f"  [{tag}] {f.path}  ({f.category}, {loc}"
        line += ", self-gitignored)" if f.gitignored else ")"
        (error if f.severity in ("critical", "high") else warn)(line)
        info(f"        why: {f.why}")
    crit = [f for f in report.findings if f.severity == "critical"]
    if crit:
        error(
            "  ROTATE the leaked secret(s) NOW. A history rewrite removes them "
            "from the repo but NOT from anyone who already fetched -- treat "
            "them as compromised."
        )
    info(f"  recommended remediation: {report.remediation()}")


# ---------------------------------------------------------------------------
# Fix
# ---------------------------------------------------------------------------

_BACKUP_PREFIX = "spill-backup"


def fix(
    cwd: Path,
    paths: list[str],
    mode: str,
    execute: bool = False,
) -> int:
    """Remediate spilled paths. Returns a process exit code.

    Modes:
      untrack  -- ``git rm --cached`` + append to .gitignore (no history touch).
      amend    -- untrack, then amend the current (unpushed) commit.
      rewrite  -- purge the paths from ALL history on a BACKUP branch via
                  git-filter-repo, then STOP: a human force-pushes the
                  protected branch (the agent must not -- tier-0 guard).

    ``execute`` False is a dry run (prints the plan). ``untrack``/``amend`` are
    reversible and run under ``--execute``; ``rewrite`` only ever prepares the
    backup + prints the human's force-push step.
    """
    if not paths:
        error("spill fix: no paths given.")
        return 2
    if mode not in ("untrack", "amend", "rewrite"):
        error(f"spill fix: unknown mode {mode!r} (untrack|amend|rewrite).")
        return 2

    if not execute:
        info(f"spill fix (DRY RUN) mode={mode} -- would remediate:")
        for p in paths:
            info(f"  - {p}")
        info("  re-run with --execute to apply.")
        if mode == "rewrite":
            _print_rewrite_plan(cwd, paths)
        return 0

    if mode in ("untrack", "amend"):
        return _fix_untrack(cwd, paths, amend=(mode == "amend"))
    return _fix_rewrite(cwd, paths)


def _fix_untrack(cwd: Path, paths: list[str], amend: bool) -> int:
    for p in paths:
        run_cmd(["git", "rm", "--cached", "-r", "--ignore-unmatch", p],
                check=False, cwd=cwd)
    _append_gitignore(cwd, paths)
    success(f"untracked {len(paths)} path(s) + added to .gitignore.")
    if amend:
        run_cmd(["git", "add", ".gitignore"], check=False, cwd=cwd)
        run_cmd(["git", "commit", "--amend", "--no-edit"], check=False, cwd=cwd)
        success("amended the current commit. Verify with `git show --stat`.")
    else:
        info("staged the removal -- commit it (`git commit`) to record the fix.")
    return 0


def _append_gitignore(cwd: Path, paths: list[str]) -> None:
    gi = cwd / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
    add = [p for p in paths if p not in existing]
    if not add:
        return
    with gi.open("a", encoding="utf-8", newline="\n") as fh:
        if existing and existing[-1].strip():
            fh.write("\n")
        fh.write("# spilled paths (hyperi-ci spill)\n")
        for p in add:
            fh.write(f"{p}\n")


def _print_rewrite_plan(cwd: Path, paths: list[str]) -> None:
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "HEAD"
    info("  rewrite plan (already-pushed leak):")
    info(f"    1. backup branch: {_BACKUP_PREFIX}/{branch}")
    info("    2. git filter-repo --invert-paths " + " ".join(f"--path {p}" for p in paths))
    info("    3. a HUMAN force-pushes the protected branch (agent must not):")
    info(f"         git push --force-with-lease origin {branch}")
    info("    4. ROTATE any leaked secret + tell collaborators to re-clone.")


def _fix_rewrite(cwd: Path, paths: list[str]) -> int:
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "HEAD"
    backup = f"{_BACKUP_PREFIX}/{branch}"
    # Safety: a clean backup ref BEFORE any rewrite.
    run_cmd(["git", "branch", "-f", backup], check=False, cwd=cwd)
    success(f"backup branch created: {backup} (rewrite is reversible from here).")

    has_fr = run_cmd(["git", "filter-repo", "--help"], check=False, capture=True, cwd=cwd)
    if has_fr.returncode != 0:
        error(
            "git-filter-repo is not installed -- it is the only safe rewriter. "
            "Install it (`uv tool install git-filter-repo` / `pip install "
            "git-filter-repo`) and re-run, or do the rewrite by hand from the "
            "backup branch."
        )
        _print_rewrite_plan(cwd, paths)
        return 1

    args = ["git", "filter-repo", "--force", "--invert-paths"]
    for p in paths:
        args += ["--path", p]
    r = run_cmd(args, check=False, cwd=cwd)
    if r.returncode != 0:
        error("filter-repo failed -- history is unchanged; restore from the backup if needed.")
        return 1
    success("history rewritten locally. The spilled paths are gone from every commit.")
    warn(
        "STOP: the final step is a force-push of a protected branch -- a "
        "shared-history rewrite. A HUMAN does this, not the agent:"
    )
    info(f"    git push --force-with-lease origin {branch}")
    info(f"    (recover if needed: git reset --hard {backup})")
    warn("Then ROTATE any leaked secret and have collaborators re-clone.")
    return 0
