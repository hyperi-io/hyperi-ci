# Project:   HyperI CI
# File:      src/hyperi_ci/quality/doc_register.py
# Purpose:   REGISTER words over every markdown line
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Check every markdown line for REGISTER words.

The whole tree is checked, not the diff. Only prose is read: markdown-it drops
code spans, fenced and indented code, HTML and link targets, and front matter
is blanked first. The mode is ``quality.doc_register``.

A doc that has to quote a banned word suppresses it with a comment:
``<!-- prose-disable-line register -->`` for one line, or
``<!-- prose-disable register -->`` for the file. A bare disable covers every
rule.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

from hyperi_ci.common import info
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.register_words import RULE, hits

_TOOL = "doc-register"
_MD = MarkdownIt("commonmark").enable("table")
_DISABLE = re.compile(r"<!--\s*(?:prose|doclint)-disable(-line)?\b([^>]*?)-->", re.I)
_BREAKS = frozenset({"softbreak", "hardbreak"})
_ALL = "*"


@dataclass(slots=True)
class Prose:
    """The prose of one markdown file and the rules its comments disable.

    Attributes:
        lines: ``(line number, text)`` for each line of prose, 1-indexed.
        file_off: Rule ids disabled for the whole file.
        line_off: Rule ids disabled per line.
    """

    lines: list[tuple[int, str]] = field(default_factory=list)
    file_off: set[str] = field(default_factory=set)
    line_off: dict[int, set[str]] = field(default_factory=dict)

    def disables(self, html: str, line: int) -> None:
        """Record each disable comment in ``html``, which starts on ``line``."""
        for offset, text in enumerate(html.split("\n")):
            for match in _DISABLE.finditer(text):
                ids = set(match.group(2).split()) or {_ALL}
                if match.group(1):
                    self.line_off.setdefault(line + offset, set()).update(ids)
                else:
                    self.file_off.update(ids)

    def is_off(self, line: int) -> bool:
        """Return True when the register rule is disabled on ``line``."""
        off = self.file_off | self.line_off.get(line, set())
        return bool({_ALL, RULE} & off)


def _without_front_matter(text: str) -> str:
    """Blank a leading YAML front-matter block, keeping the line count."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    for end in range(1, len(lines)):
        if lines[end].strip() in ("---", "..."):
            return "\n" * end + "\n".join(lines[end:])
    return text


def _inline(token: Token, prose: Prose) -> Iterator[tuple[int, str]]:
    """Yield each line of prose in one inline token."""
    line = token.map[0] + 1 if token.map else 1
    parts: list[str] = []
    for child in token.children or []:
        if child.type in _BREAKS:
            yield line, "".join(parts)
            parts, line = [], line + 1
        elif child.type == "text":
            parts.append(child.content)
        elif child.type == "html_inline":
            prose.disables(child.content, line)
        else:
            parts.append(" ")
    yield line, "".join(parts)


def prose_of(text: str) -> Prose:
    """Return the prose lines of the markdown ``text`` and its disables."""
    prose = Prose()
    for token in _MD.parse(_without_front_matter(text)):
        if token.type == "html_block" and token.map:
            prose.disables(token.content, token.map[0] + 1)
        elif token.type == "inline" and token.map:
            prose.lines.extend((n, t) for n, t in _inline(token, prose) if t.strip())
    return prose


def scan(path: Path, root: Path) -> list[fdg.Finding]:
    """Return a finding per REGISTER word on each prose line of ``path``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    prose = prose_of(text)
    return [
        fdg.Finding(
            tool=_TOOL,
            path=fdg.relpath(path, root),
            line=line,
            level="error",
            rule=RULE,
            message=f"{word.term!r}: {word.replacement}",
        )
        for line, content in prose.lines
        if not prose.is_off(line)
        for word in hits(content)
    ]


def run(
    files: list[Path],
    config: CIConfig,
    *,
    root: Path | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Check ``files`` for REGISTER words, and return the exit code.

    Returns 1 when the check is blocking and finds a hit. A generated
    CHANGELOG.md is skipped, because the commit check already reads its
    subjects.
    """
    mode = resolve_tool_mode("doc_register", config, default="warn")
    if mode == "disabled":
        info(f"  {_TOOL}: disabled")
        return 0
    root = Path(root or Path.cwd())
    prose = [p for p in files if p.name.lower() != "changelog.md"]
    found = [f for path in prose for f in scan(path, root)]
    fdg.report(_TOOL, found, mode, sarif_path=sarif_path)
    return fdg.verdict(_TOOL, len(found), mode, f"no hits in {len(prose)} file(s)")
