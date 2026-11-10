# Project:   HyperI CI
# File:      src/hyperi_ci/quality/bsd_text.py
# Purpose:   Name the BSD licence a licence file holds, by full-text match
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Name the BSD licence a licence file holds, by full-text match.

The ``BSD License`` trove classifier covers BSD-4-Clause too, so it names no
licence on its own. A file whose whole text, copyright lines aside, is the
BSD-2-Clause, BSD-3-Clause, 0BSD or ISC clause text is that licence. Anything
left over, an added clause or a line of prose, is no match.
"""

import re

# Licence metadata that says BSD without saying which.
BARE_BSD = frozenset({"BSD", "BSD LICENSE"})

# A copyright or rights line, not a clause wrapped to start "copyright notice".
_COPYRIGHT = re.compile(
    r"^\W*(copyright\b(?!\s+(notice|holder|owner))|\(c\)|©|all rights reserved)",
    re.IGNORECASE,
)
_TITLE = re.compile(
    r"^\W*(the )?(((new|simplified|modified|revised) )?bsd( [23]-clause| zero[- ]clause)?"
    r"|isc)( licen[cs]e)?( \(iscl\))?\W*$",
    re.IGNORECASE,
)
# Lines no clause can hide in: an rst underline, "License for <package>", and a
# bare "(see <url>)" pointer under a copyright line.
_DECORATION = re.compile(
    r"^\W*$|^\W*licen[cs]e for [\w.-]+\W*$|^\s*\(see https?://\S+\)\.?\s*$",
    re.IGNORECASE,
)

# A name slot: one clause, at most 80 characters, no wording that adds a condition.
_HOLDER = (
    r"(?:(?!\b(?:may|must|shall|not|no|only|unless|provided|except|permitted"
    r"|prohibited|fee|commercial)\b)[^;:]){1,80}?"
)
_CONDITIONS = (
    r"redistribution and use in source and binary forms, with or without "
    r"modification, are permitted provided that the following conditions are met: "
    r"redistributions of source code must retain the above copyright notice, this "
    r"list of conditions and the following disclaimer\. redistributions in binary "
    r"form must reproduce the above copyright notice, this list of conditions and "
    r"the following disclaimer in the documentation and/or other materials provided "
    r"with the distribution\. "
)
_NAME_CLAUSE = (
    rf"neither the name of {_HOLDER} nor the names of (its|their) contributors may "
    r"be used to endorse or promote products derived from this software without "
    r"specific prior written permission\. "
)
_DISCLAIMER = (
    rf"this software is provided by {_HOLDER} as is and any express or implied "
    r"warranties, including, but not limited to, the implied warranties of "
    r"merchantability and fitness for a particular purpose are disclaimed\. in no "
    rf"event shall {_HOLDER} be liable for any direct, indirect, incidental, "
    r"special, exemplary, or consequential damages \(including, but not limited to, "
    r"procurement of substitute goods or services; loss of use, data, or profits; or "
    r"business interruption\) however caused and on any theory of liability, whether "
    r"in contract, strict liability, or tort \(including negligence or otherwise\) "
    r"arising in any way out of the use of this software, even if advised of the "
    r"possibility of such damage\."
)
_GRANT = (
    r"permission to use, copy, modify, and(/or)? distribute this software for any "
    r"purpose with or without fee is hereby granted"
)
# ISC is the 0BSD grant plus this one condition.
_ISC_NOTICE = (
    r", provided that the above copyright notice and this permission notice appear "
    r"in all copies"
)
_WARRANTY = (
    r"\. the software is provided as is "
    r"and the author disclaims all warranties with regard to this software including "
    r"all implied warranties of merchantability and fitness\. in no event shall the "
    r"author be liable for any special, direct, indirect, or consequential damages or "
    r"any damages whatsoever resulting from loss of use, data or profits, whether in "
    r"an action of contract, negligence or other tortious action, arising out of or "
    r"in connection with the use or performance of this software\."
)
_TEMPLATES = {
    "BSD-2-Clause": re.compile(_CONDITIONS + _DISCLAIMER),
    "BSD-3-Clause": re.compile(_CONDITIONS + _NAME_CLAUSE + _DISCLAIMER),
    "0BSD": re.compile(_GRANT + _WARRANTY),
    "ISC": re.compile(_GRANT + _ISC_NOTICE + _WARRANTY),
}

# List markers (`1.`, `a)`, `(ii)`, `-`) and markdown emphasis.
_MARKERS = (
    re.compile(r"(?<![\w(])(\d+|[a-z]|[ivx]+)[.)](?=\s)"),
    re.compile(r"(?<![\w(])\((\d+|[a-z]|[ivx]+)\)(?=\s)"),
    re.compile(r"(?<!\S)-(?=\s)"),
    re.compile(r"[*#_>|]"),
)
_QUOTES = re.compile(
    "[\"'`\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"
    "\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}]"
)
_SPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    kept = [
        line
        for line in text.splitlines()
        if not any(pattern.match(line) for pattern in (_COPYRIGHT, _TITLE, _DECORATION))
    ]
    out = _QUOTES.sub("", " ".join(kept).lower())
    for marker in _MARKERS:
        out = marker.sub(" ", out)
    return _SPACE.sub(" ", out).strip()


def bsd_licence(text: str | None) -> str | None:
    """Return ``BSD-2-Clause``, ``BSD-3-Clause``, ``0BSD`` or ``ISC`` when that is all ``text`` holds.

    Args:
        text: A licence file's content.

    Returns:
        The SPDX id, or None when anything beyond copyright lines and one of
        the clause texts remains.

    """
    body = _normalise(text or "")
    return next(
        (spdx for spdx, template in _TEMPLATES.items() if template.fullmatch(body)),
        None,
    )
