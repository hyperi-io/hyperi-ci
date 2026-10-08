# Project:   HyperI CI
# File:      src/hyperi_ci/quality/doc_paths.py
# Purpose:   Catch docs that still name a file the repo no longer has
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Report doc references to files the repo no longer has; needs nothing installed.

* A markdown link destination that is gone is an error.
* An inline-code path that is gone is a warning. To count, the token needs two
  or more segments and a file extension, an existing parent directory, and a
  parent holding another file of the same extension. That drops routes
  (``/healthz``), slash-commands, bare directories, ``application/json``, and
  paths in another repo's layout (``src/main.rs`` where ``src/`` holds no
  ``.rs``).

Docs under ``quality.doc_paths.prescriptive`` name paths in consumer trees, so
their inline code is not checked; their links still are. A line carrying
``<!-- doc-paths: ignore -->`` is suppressed, and the run counts suppressions.
The link rule defers to lychee when lychee runs at the same mode or stricter.
"""

import re
from pathlib import Path, PurePosixPath

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode, stricter
from hyperi_ci.quality import findings as fdg

# Inline `[text](dest)` / `![alt](dest)`, plus the `[id]: dest` reference form.
_INLINE_LINK = re.compile(r"(?<!\\)!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
_REF_LINK = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+)>?", re.MULTILINE)

# Single-backtick spans only: multi-backtick spans hold code samples.
_CODE_SPAN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")

# Fenced blocks are removed first: paths in samples make no claim about the repo.
_FENCED = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1", re.MULTILINE | re.DOTALL)

# Placeholder and glob characters: such a token names no literal path.
_NOT_LITERAL = re.compile(r"[*?{}<>$()\[\]|!\s]")

_NON_PATH_PREFIX = ("#", "//", "mailto:", "tel:", "data:")

_PRESCRIPTIVE = "quality.doc_paths.prescriptive"

_IGNORE_MARKER = re.compile(r"<!--\s*doc-paths:\s*ignore\s*-->")


class _Suppressed:
    """A suppression count threaded through the scan functions' keyword args."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


def _line_at(text: str, pos: int) -> str:
    """Return the line of ``text`` containing offset ``pos``, newline excluded."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start : end if end != -1 else len(text)]


def _is_marked(text: str, pos: int) -> bool:
    """Return True when the line at ``pos`` carries the ``doc-paths: ignore`` marker."""
    return _IGNORE_MARKER.search(_line_at(text, pos)) is not None


def prescriptive_dirs(config: CIConfig) -> list[PurePosixPath]:
    """Return ``quality.doc_paths.prescriptive``, or raise on a malformed value.

    Raises:
        ValueError: The value is not a list of repo-relative directory paths.
    """
    raw = config.get(_PRESCRIPTIVE, [])
    if not isinstance(raw, list):
        raise ValueError(
            f"{_PRESCRIPTIVE} must be a list of repo-relative directories, "
            f"got {type(raw).__name__}: {raw!r}"
        )
    dirs: list[PurePosixPath] = []
    for entry in raw:
        path = (
            PurePosixPath(entry.strip().removeprefix("./"))
            if isinstance(entry, str)
            else None
        )
        # `.` would exempt the whole repo, which is `doc_paths: disabled`.
        if (
            path is None
            or not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or _NOT_LITERAL.search(str(path))
        ):
            raise ValueError(
                f"{_PRESCRIPTIVE} entries must be repo-relative directory paths "
                f"with no globs, got {entry!r}"
            )
        dirs.append(path)
    return dirs


def is_prescriptive(doc: Path, root: Path, dirs: list[PurePosixPath]) -> bool:
    """Return True when ``doc`` sits inside one of ``dirs``, measured from ``root``.

    Matched by whole segment: ``docs`` covers ``docs/a.md``, not ``docs-old/a.md``.
    """
    try:
        relative = doc.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    posix = PurePosixPath(relative.as_posix())
    return any(posix.is_relative_to(d) for d in dirs)


def _is_repo_path(dest: str) -> bool:
    """Return True when ``dest`` is a relative path this repo could hold."""
    if not dest or dest.startswith(_NON_PATH_PREFIX) or "://" in dest:
        return False
    return not _NOT_LITERAL.search(dest)


def _resolve(token: str, doc: Path, root: Path) -> bool:
    """Return True when ``token`` resolves, relative to the doc or the repo root.

    A leading ``/`` is GitHub's repo-root form, so it is tried from the root only.
    """
    candidates = [root / token.lstrip("/")]
    if not token.startswith("/"):
        candidates.append(doc.parent / token)
    return any(c.exists() for c in candidates)


def _strip_fragment(dest: str) -> str:
    """Drop a ``#anchor`` / ``?query`` suffix, leaving the path to resolve."""
    return dest.split("#", 1)[0].split("?", 1)[0]


def scan_links(
    doc: Path, root: Path, *, suppressed: _Suppressed | None = None
) -> list[fdg.Finding]:
    """Return one finding per markdown link in ``doc`` whose target is gone.

    A link on a ``doc-paths: ignore`` line is skipped and tallied in ``suppressed``.
    """
    try:
        text = doc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Code such as a C++ lambda capture parses as markdown link syntax.
    text = _FENCED.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out: list[fdg.Finding] = []
    seen: set[str] = set()
    for pattern in (_INLINE_LINK, _REF_LINK):
        for match in pattern.finditer(text):
            dest = _strip_fragment(match.group(1))
            if not _is_repo_path(dest) or dest in seen:
                continue
            if _resolve(dest, doc, root):
                seen.add(dest)
                continue
            if _is_marked(text, match.start()):
                # Not added to `seen`, so a later unmarked occurrence is reported.
                if suppressed is not None:
                    suppressed.count += 1
                continue
            seen.add(dest)
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
    """Return True when ``token`` has two or more segments and a file extension."""
    if token.endswith("/"):
        return False
    relative = token.lstrip("/").removeprefix("./")
    parts = [p for p in relative.split("/") if p and p != "."]
    return len(parts) >= 2 and bool(Path(parts[-1]).suffix)


def _siblings_share_the_kind(token: str, doc: Path, root: Path) -> bool:
    """Return True when the token's parent dir holds another file of its type."""
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


def scan_code_paths(
    doc: Path, root: Path, *, suppressed: _Suppressed | None = None
) -> list[fdg.Finding]:
    """Return one finding per inline-code path in ``doc`` that no longer exists.

    Filtered by :func:`looks_like_a_file` and :func:`_siblings_share_the_kind`.
    A token on a ``doc-paths: ignore`` line is skipped and tallied in ``suppressed``.
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
        if _resolve(token, doc, root):
            seen.add(token)
            continue
        try:
            if not _siblings_share_the_kind(token, doc, root):
                seen.add(token)
                continue
        except OSError:
            seen.add(token)
            continue
        if _is_marked(stripped, match.start()):
            # Not added to `seen`, so a later unmarked occurrence is reported.
            if suppressed is not None:
                suppressed.count += 1
            continue
        seen.add(token)
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

    ``lychee_mode`` is the mode lychee checks links at this run, or None when
    it does not run. Links are left to lychee unless this check is stricter.

    Returns 0 unless a blocking mode found an error-level finding, or
    ``quality.doc_paths.prescriptive`` is malformed.
    """
    mode = resolve_tool_mode("doc_paths", config, default="warn")
    if mode == "disabled":
        info("  doc-paths: disabled")
        return 0
    if not files:
        info("  doc-paths: no markdown to check - skipping")
        return 0
    try:
        prescriptive = prescriptive_dirs(config)
    except ValueError as exc:
        error(f"  doc-paths: {exc}")
        return 1

    check_links = lychee_mode is None or stricter(mode, than=lychee_mode)
    root = Path(root or Path.cwd())
    found: list[fdg.Finding] = []
    exempt = 0
    suppressed = _Suppressed()
    for doc in files:
        if check_links:
            found.extend(scan_links(doc, root, suppressed=suppressed))
        if is_prescriptive(doc, root, prescriptive):
            exempt += 1
            continue
        found.extend(scan_code_paths(doc, root, suppressed=suppressed))
    if exempt:
        info(
            f"  doc-paths: inline-code paths not checked in {exempt} file(s) "
            f"({_PRESCRIPTIVE})"
        )
    if suppressed.count:
        info(
            f"  doc-paths: {suppressed.count} reference(s) ignored by marker "
            "(doc-paths: ignore)"
        )

    fdg.report("doc-paths", found, mode, sarif_path=sarif_path)

    if not found:
        success(f"  doc-paths: every path named in {len(files)} file(s) resolves")
        return 0
    errors = [f for f in found if f.level == "error"]
    if mode == "blocking" and errors:
        error(f"  doc-paths: {len(errors)} doc(s) point at a path that is gone")
        return 1
    warn(f"  doc-paths: {len(found)} stale path reference(s) (non-blocking)")
    return 0
