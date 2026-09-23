# Project:   HyperI CI
# File:      src/hyperi_ci/quality/doc_paths.py
# Purpose:   Catch docs that still name a file the repo no longer has
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Path-existence drift - the docs check that needs nothing installed.

Docs rot by naming files that have since moved or gone. A rename lands, the
prose still points at the old path, and the only signal is a reader who cannot
find it. The fix is mechanical: resolve every path a doc names and report the
ones that are no longer there.

Two kinds of reference, because they fail differently:

* a markdown LINK destination (``[text](docs/thing.md)``) - a reader clicks it
  and gets a 404. Reported as an error.
* a path in INLINE CODE (``` `src/hyperi_ci/quality/hadolint.py` ```) - the
  prose is simply wrong about where something lives. Reported as a warning:
  lower stakes, and the shape is inferred rather than declared.

The inline-code rule is the one that needs a false-positive filter, because
``application/json``, ``/metrics`` and ``dist/`` are slash-separated tokens too.
Three conditions, each removing a class of non-path:

* **Two or more segments, and a file extension.** Kills the routes
  (``/healthz``), the slash-commands (``/deps``) and the bare directory names
  (``target/``, ``.claude/``), none of which claim a file exists.
* **The parent directory exists.** ``application/json`` never had a parent
  directory, so it was never a path.
* **The parent holds another file of the same kind.** This is what separates
  drift from a doc describing somebody ELSE's tree: ``config/org.yaml`` is
  missing from a directory full of YAML, so it moved; ``src/main.rs`` names a
  consumer's Rust layout, and this repo's ``src/`` has never held a ``.rs``.

lychee owns link destinations when it is installed (it also resolves anchors,
which this cannot), so the link rule turns off where lychee runs at the same
mode or stricter. Where this check is the stricter of the two, it keeps the
rule, because a gate a repo promoted cannot be decided by one it did not.
"""

import re
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode, stricter
from hyperi_ci.quality import findings as fdg

# Inline `[text](dest)` / `![alt](dest)`, plus the `[id]: dest` reference form.
_INLINE_LINK = re.compile(r"(?<!\\)!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
_REF_LINK = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+)>?", re.MULTILINE)

# A single-backtick span. Multi-backtick spans hold code samples, not paths.
_CODE_SPAN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")

# A fenced code block. Paths inside a shell sample are illustrative, not claims
# about the repo, so they are removed before the code spans are read.
_FENCED = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1", re.MULTILINE | re.DOTALL)

# Placeholders and globs: the token is a TEMPLATE, so no literal path exists.
_NOT_LITERAL = re.compile(r"[*?{}<>$()\[\]|!\s]")

# A destination that is not a repo path at all.
_NON_PATH_PREFIX = ("#", "//", "mailto:", "tel:", "data:")


def _is_repo_path(dest: str) -> bool:
    """Return True when ``dest`` is a relative path this repo could hold."""
    if not dest or dest.startswith(_NON_PATH_PREFIX) or "://" in dest:
        return False
    return not _NOT_LITERAL.search(dest)


def _resolve(token: str, doc: Path, root: Path) -> bool:
    """Return True when ``token`` resolves, relative to the doc or the repo root.

    Both anchors are tried because docs use both: a sibling link is written
    relative to the doc, while prose citing ``src/...`` means from the root. A
    leading ``/`` is GitHub's repo-root form, not a filesystem absolute.
    """
    candidates = [root / token.lstrip("/")]
    if not token.startswith("/"):
        candidates.append(doc.parent / token)
    return any(c.exists() for c in candidates)


def _strip_fragment(dest: str) -> str:
    """Drop a ``#anchor`` / ``?query`` suffix, leaving the path to resolve."""
    return dest.split("#", 1)[0].split("?", 1)[0]


def scan_links(doc: Path, root: Path) -> list[fdg.Finding]:
    """Return one finding per markdown link in ``doc`` whose target is gone."""
    try:
        text = doc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # A C++ lambda capture and a Python generic parameter are both valid
    # markdown link syntax, so a fenced block reads as links to code.
    text = _FENCED.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out: list[fdg.Finding] = []
    seen: set[str] = set()
    for pattern in (_INLINE_LINK, _REF_LINK):
        for match in pattern.finditer(text):
            dest = _strip_fragment(match.group(1))
            if not _is_repo_path(dest) or dest in seen:
                continue
            seen.add(dest)
            if _resolve(dest, doc, root):
                continue
            out.append(
                fdg.Finding(
                    tool="doc-paths",
                    path=str(doc),
                    line=text.count("\n", 0, match.start()) + 1,
                    level="error",
                    rule="docs/link-missing",
                    message=f"links to `{dest}`, which does not exist",
                )
            )
    return out


def looks_like_a_file(token: str) -> bool:
    """Return True when ``token`` claims a specific file rather than a directory.

    Two or more segments and a file extension. A route (``/metrics``), a
    slash-command (``/deps``) and a bare directory (``target/``) all fail it,
    and none of them asserts that anything exists on disk.
    """
    if token.endswith("/"):
        return False
    relative = token.lstrip("/").removeprefix("./")
    parts = [p for p in relative.split("/") if p and p != "."]
    return len(parts) >= 2 and bool(Path(parts[-1]).suffix)


def _siblings_share_the_kind(token: str, doc: Path, root: Path) -> bool:
    """Return True when the token's parent dir holds another file of its type.

    The discriminator between drift and a doc describing a different repo: a
    ``.yaml`` missing from a directory of YAML moved, while a ``.rs`` named
    under a directory that has never held one belongs to somebody else's tree.
    """
    relative = Path(token.lstrip("/").removeprefix("./"))
    suffix = relative.suffix.lower()
    for anchor in (root, doc.parent):
        parent = anchor / relative.parent
        if not parent.is_dir():
            continue
        if any(
            child.is_file() and child.suffix.lower() == suffix
            for child in parent.iterdir()
        ):
            return True
    return False


def scan_code_paths(doc: Path, root: Path) -> list[fdg.Finding]:
    """Return one finding per inline-code path in ``doc`` that no longer exists.

    Filtered by :func:`looks_like_a_file` and :func:`_siblings_share_the_kind` -
    see the module docstring for what each one removes.
    """
    try:
        text = doc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    stripped = _FENCED.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out: list[fdg.Finding] = []
    seen: set[str] = set()
    for match in _CODE_SPAN.finditer(stripped):
        token = match.group(1).strip()
        if token in seen or not _is_repo_path(token) or not looks_like_a_file(token):
            continue
        seen.add(token)
        if _resolve(token, doc, root):
            continue
        try:
            if not _siblings_share_the_kind(token, doc, root):
                continue
        except OSError:
            continue
        out.append(
            fdg.Finding(
                tool="doc-paths",
                path=str(doc),
                line=stripped.count("\n", 0, match.start()) + 1,
                level="warning",
                rule="docs/path-missing",
                message=f"names `{token}`, which is no longer in the repo",
            )
        )
    return out


def run(
    files: list[Path],
    config: CIConfig,
    *,
    root: Path | None = None,
    lychee_mode: str | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Report the paths ``files`` name that the repo no longer has.

    ``lychee_mode`` is the mode lychee checks link destinations at this run,
    or None when it does not run. The link rule is left to lychee unless this
    check gates harder, so a broken link is reported once where the two agree
    and still fails a ``doc_paths: blocking`` repo whose lychee only warns.

    Returns 0 unless a blocking mode found an error-level finding.
    """
    mode = resolve_cross_tool_mode(config, "doc_paths", "warn")
    if mode == "disabled":
        info("  doc-paths: disabled")
        return 0
    if not files:
        info("  doc-paths: no markdown to check - skipping")
        return 0

    check_links = lychee_mode is None or stricter(mode, than=lychee_mode)
    root = Path(root or Path.cwd())
    found: list[fdg.Finding] = []
    for doc in files:
        if check_links:
            found.extend(scan_links(doc, root))
        found.extend(scan_code_paths(doc, root))

    dropped = fdg.surface("doc-paths", found, sarif_path=sarif_path)
    if dropped:
        info(f"  doc-paths: +{dropped} more finding(s) in the job summary")

    if not found:
        success(f"  doc-paths: every path named in {len(files)} file(s) resolves")
        return 0
    errors = [f for f in found if f.level == "error"]
    if mode == "blocking" and errors:
        error(f"  doc-paths: {len(errors)} doc(s) point at a path that is gone")
        return 1
    warn(f"  doc-paths: {len(found)} stale path reference(s) (non-blocking)")
    return 0
