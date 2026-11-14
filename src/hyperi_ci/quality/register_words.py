# Project:   HyperI CI
# File:      src/hyperi_ci/quality/register_words.py
# Purpose:   Find REGISTER words in a commit subject, PR title or markdown line
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Find REGISTER words in a commit subject, PR title or markdown line.

The word list is the packaged ``config/register-words.yaml``. In a commit
subject or PR title a hit is a warning, never a failure. lint-docs applies the
same list to markdown.
"""

import re
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

import yaml

_TABLE_PATH = Path(__file__).resolve().parents[1] / "config" / "register-words.yaml"

# The rule id a docs finding carries, and a prose-disable comment names.
RULE = "register"


@dataclass(frozen=True, slots=True)
class RegisterWord:
    """One REGISTER word.

    Attributes:
        term: The label a warning names.
        pattern: Python regex, matched case-insensitive.
        replacement: What to write instead.
    """

    term: str
    pattern: str
    replacement: str


@cache
def _table() -> tuple[tuple[RegisterWord, re.Pattern[str]], ...]:
    data = yaml.safe_load(_TABLE_PATH.read_text(encoding="utf-8"))
    entries = []
    for row in data["words"]:
        word = RegisterWord(row["term"], row["pattern"], row["replacement"])
        entries.append((word, re.compile(word.pattern, re.IGNORECASE)))
    return tuple(entries)


def words() -> list[dict[str, str]]:
    """Return the word list as plain mappings, as ``--register-words`` prints it."""
    return [asdict(word) for word, _ in _table()]


# Mentions, not use: a code span, a token holding a path separator, and a
# leading conventional-commit type and scope.
_NOT_PROSE = (
    re.compile(r"(`+).*?\1"),
    re.compile(r"\S*/\S*"),
    re.compile(r"^\s*[a-z]+(?:\([^)]*\))?!?:\s"),
)


def prose(line: str) -> str:
    """Return ``line`` with what no REGISTER row judges blanked out."""
    for pattern in _NOT_PROSE:
        line = pattern.sub(" ", line)
    return line


def hits(line: str) -> list[RegisterWord]:
    """Return the REGISTER words the prose of ``line`` carries, in table order."""
    text = prose(line)
    return [word for word, regex in _table() if regex.search(text)]


def describe(line: str) -> list[str]:
    """Return one warning line per REGISTER word in ``line``."""
    return [f"register word {w.term!r}: {w.replacement}" for w in hits(line)]
