# Project:   HyperI CI
# File:      src/hyperi_ci/quality/register_words.py
# Purpose:   Find REGISTER words in a commit subject or PR title
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Find REGISTER words in a commit subject or PR title.

The word list is the packaged ``config/register-words.yaml``. A hit is a
warning, never a failure.
"""

import re
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

import yaml

_TABLE_PATH = Path(__file__).resolve().parents[1] / "config" / "register-words.yaml"


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


def hits(line: str) -> list[RegisterWord]:
    """Return the REGISTER words ``line`` carries, in table order."""
    return [word for word, regex in _table() if regex.search(line)]


def describe(line: str) -> list[str]:
    """Return one warning line per REGISTER word in ``line``."""
    return [f"register word {w.term!r}: {w.replacement}" for w in hits(line)]
