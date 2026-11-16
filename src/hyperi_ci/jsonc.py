# Project:   HyperI CI
# File:      src/hyperi_ci/jsonc.py
# Purpose:   Read JSON that carries comments and trailing commas
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""JSON with ``//`` and ``/* */`` comments and trailing commas (JSONC).

Covers what Renovate and markdownlint-cli2 configs use. Single-quoted strings
and unquoted keys are not handled, and fail the parse.
"""

import json


def strip_comments_and_trailing_commas(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments and trailing commas outside strings."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
        elif ch == ",":
            j = i + 1
            while j < n:
                if text[j].isspace():
                    j += 1
                elif text.startswith("//", j):
                    while j < n and text[j] != "\n":
                        j += 1
                elif text.startswith("/*", j):
                    end = text.find("*/", j + 2)
                    j = n if end == -1 else end + 2
                else:
                    break
            if j < n and text[j] in "}]":
                i += 1
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def loads(text: str) -> object:
    """Parse JSONC text.

    Raises:
        json.JSONDecodeError: Not JSON once comments and trailing commas go.
    """
    return json.loads(strip_comments_and_trailing_commas(text))
