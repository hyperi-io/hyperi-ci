# Project:   HyperI CI
# File:      src/hyperi_ci/quality/charset.py
# Purpose:   Catch typographic characters the ASCII-only rule bans
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Report source characters the ASCII-only rule bans.

160 licence headers accumulated an em-dash before anyone noticed, because
nothing enforced the rule. A rule nothing enforces decays, and this is the
check that was missing (issue #169).

Scope is deliberately narrow. It reports the typographic substitutions a
keyboard cannot produce -- the dash family, curly quotes and primes,
guillemets, the one-character ellipsis, bullet, arrows, box-drawing -- and says
what to type instead. It does NOT report every non-ASCII byte: a maths section,
a name with a diacritic and a deliberate replacement character are all
legitimate, and a check that fires on them would be turned off within a week.

Two classes earn their place for a second reason. Box-drawing, because diagrams
belong in mermaid, so a box-drawing run in a docstring breaks two rules at
once. The no-break spaces, because they are the only entries here that change
what a parser does: one inside a YAML key or a TOML value is read as part of
the token, and nothing in the diff shows why the file stopped loading.
"""

from pathlib import Path

from hyperi_ci.common import info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg

_TOOL = "charset"

# Character to what a keyboard types instead. Every entry is a substitution a
# writer meant as punctuation, never a character carrying meaning of its own.
# Named escapes rather than literals, because a table whose entries render as
# their own replacements cannot be checked by reading it.
BANNED: dict[str, str] = {
    "\N{EM DASH}": "--",
    "\N{EN DASH}": "-",
    "\N{HORIZONTAL BAR}": "--",
    "\N{HYPHEN}": "-",
    "\N{NON-BREAKING HYPHEN}": "-",
    "\N{FIGURE DASH}": "-",
    "\N{MINUS SIGN}": "-",
    "\N{LEFT SINGLE QUOTATION MARK}": "'",
    "\N{RIGHT SINGLE QUOTATION MARK}": "'",
    "\N{LEFT DOUBLE QUOTATION MARK}": '"',
    "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
    "\N{PRIME}": "'",
    "\N{DOUBLE PRIME}": '"',
    "\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}": "<<",
    "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}": ">>",
    "\N{HORIZONTAL ELLIPSIS}": "...",
    "\N{BULLET}": "-",
    "\N{RIGHTWARDS ARROW}": "->",
    "\N{LEFTWARDS ARROW}": "<-",
    "\N{SECTION SIGN}": "Section",
    "\N{DAGGER}": "*",
    "\N{DOUBLE DAGGER}": "**",
}

# Spaces that are not the space bar. A no-break space inside a YAML key or a
# TOML value parses as part of the token, and the diff looks identical to the
# line that worked.
INVISIBLE: dict[str, str] = {
    "\N{NO-BREAK SPACE}": "NO-BREAK SPACE",
    "\N{NARROW NO-BREAK SPACE}": "NARROW NO-BREAK SPACE",
    "\N{THIN SPACE}": "THIN SPACE",
}

# Box-drawing and the arrow glyphs that go with it. Reported as one class
# because the fix is the same: draw it in mermaid, or write it in plain text.
# Literals, because none of these has an ASCII look-alike to confuse.
_BOX_DRAWING = "─│┌┐└┘├┤┬┴┼▼▲▶◀►"

SUFFIXES = {".py", ".yaml", ".yml", ".sh", ".toml", ".mjs"}


def scan_text(path: str, text: str) -> list[fdg.Finding]:
    """Return one finding per line carrying a banned character."""
    out: list[fdg.Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        seen = {char for char in line if char in BANNED}
        if seen:
            swaps = ", ".join(f"{char!r} -> {BANNED[char]!r}" for char in sorted(seen))
            out.append(
                fdg.Finding(
                    tool=_TOOL,
                    path=path,
                    line=number,
                    level="warning",
                    rule="charset/banned-typography",
                    message=f"type the keyboard form instead: {swaps}",
                )
            )
        hidden = {char for char in line if char in INVISIBLE}
        if hidden:
            named = ", ".join(sorted(INVISIBLE[char] for char in hidden))
            out.append(
                fdg.Finding(
                    tool=_TOOL,
                    path=path,
                    line=number,
                    level="warning",
                    rule="charset/invisible-space",
                    message=(
                        f"this line carries {named} where it reads as a space. "
                        f"YAML and TOML parse it as part of the token; replace "
                        f"it with the space bar."
                    ),
                )
            )
        if any(char in _BOX_DRAWING for char in line):
            out.append(
                fdg.Finding(
                    tool=_TOOL,
                    path=path,
                    line=number,
                    level="warning",
                    rule="charset/ascii-art",
                    message=(
                        "box-drawing characters -- draw the diagram in mermaid, "
                        "or describe it in plain text"
                    ),
                )
            )
    return out


def scan(roots: list[Path]) -> list[fdg.Finding]:
    """Scan every source file under ``roots``, except this module.

    ``_BOX_DRAWING`` holds its characters as literal table DATA, so scanning
    this file reports the definition rather than a defect -- and a check that
    flags its own dictionary teaches the reader to distrust it.
    """
    out: list[fdg.Finding] = []
    # Resolved, not by name: matching `charset.py` anywhere would exempt a
    # consumer's own module of that name, and anyone who wanted the exemption.
    this_file = Path(__file__).resolve()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            if path.resolve() == this_file:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.extend(scan_text(str(path), text))
    return out


def run(
    config: CIConfig,
    *,
    roots: list[Path] | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Report banned typography. Returns exit code.

    Args:
        config: Merged CI configuration.
        roots: Directories to scan; defaults to ``src/`` and ``scripts/``.
        sarif_path: Where to write SARIF, when the caller collects it.

    Returns:
        0 unless a blocking mode found something.

    """
    mode = resolve_cross_tool_mode(config, "charset", "warn")
    if mode == "disabled":
        info(f"  {_TOOL}: disabled")
        return 0

    # `.github/` carries the workflow and action headers this check was raised
    # about, so leaving it out reported clean on the motivating surface.
    targets = roots or [Path("src"), Path("scripts"), Path(".github")]
    found = scan(targets)

    dropped = fdg.surface(_TOOL, found, sarif_path=sarif_path)
    if dropped:
        info(f"  {_TOOL}: +{dropped} more finding(s) in the job summary")

    if not found:
        success(f"  {_TOOL}: no banned typography")
        return 0
    if mode == "blocking":
        return 1
    warn(f"  {_TOOL}: {len(found)} line(s) carry banned typography -- advisory")
    return 0
