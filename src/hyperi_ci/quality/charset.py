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

import os
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from hyperi_ci.common import error, get_exclude_dirs, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality import targets

_TOOL = "charset"
_EXCLUDE_PATHS = "quality.exclude_paths"
_CHARSET_EXCLUDE = "quality.charset_exclude"

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


@dataclass(frozen=True, slots=True)
class Selection:
    """The files to scan, and how many each exclusion source dropped.

    Attributes:
        files: Files to scan, sorted within each root.
        excluded: Dropped-file count per config key, for keys that dropped any.
    """

    files: list[Path]
    excluded: Counter[str]


def exclude_patterns(config: CIConfig) -> list[str]:
    """Return ``quality.charset_exclude``, or raise on a malformed value.

    A malformed exclusion would otherwise leave the check scanning what the
    repo meant to exclude, with nothing saying why.

    Raises:
        ValueError: The value is not a list of relative glob strings.
    """
    raw = config.get(_CHARSET_EXCLUDE, [])
    if not isinstance(raw, list):
        raise ValueError(
            f"{_CHARSET_EXCLUDE} must be a list of glob patterns, "
            f"got {type(raw).__name__}: {raw!r}"
        )
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip() or entry.startswith("/"):
            raise ValueError(
                f"{_CHARSET_EXCLUDE} entries must be non-empty repo-relative glob "
                f"strings, got {entry!r}"
            )
    return raw


def select(
    roots: list[Path],
    *,
    base: Path | None = None,
    exclude_dirs: Iterable[str] = (),
    exclude_paths: Iterable[str] = (),
    patterns: Iterable[str] = (),
) -> Selection:
    """Pick the files under ``roots`` to scan, except this module.

    ``_BOX_DRAWING`` holds its characters as literal table DATA, so scanning
    this file reports the definition rather than a defect -- and a check that
    flags its own dictionary teaches the reader to distrust it.

    Args:
        roots: Directories to walk.
        base: The repo root that relative paths and globs are measured from;
            defaults to the working directory.
        exclude_dirs: Directories pruned without being counted, on top of the
            set :mod:`hyperi_ci.quality.targets` always prunes.
        exclude_paths: Directories from ``quality.exclude_paths``, matched the
            same way (a bare name or a relative path) and counted.
        patterns: Globs from ``quality.charset_exclude``. Each matches the whole
            repo-relative POSIX path, and ``**`` spans directories.

    Returns:
        The files to scan and the per-source excluded counts.
    """
    base = Path.cwd() if base is None else base
    pruned = targets.prune_set(exclude_dirs)
    configured = targets.exclude_set(exclude_paths)
    globs = list(patterns)
    # Resolved, not by name: matching `charset.py` anywhere would exempt a
    # consumer's own module of that name, and anyone who wanted the exemption.
    this_file = Path(__file__).resolve()
    files: list[Path] = []
    excluded: Counter[str] = Counter()
    for root in roots:
        if not root.is_dir():
            continue
        kept: list[Path] = []
        # Whether each directory still to be walked sits inside an
        # exclude_paths entry, keyed the way os.walk reports it.
        inside = {os.fspath(root): targets.is_pruned(base / root, base, configured)}
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            anchored = base / here
            excluded_here = inside.pop(dirpath)
            dirnames[:] = [
                d for d in dirnames if not targets.is_pruned(anchored / d, base, pruned)
            ]
            for d in dirnames:
                below = targets.is_pruned(anchored / d, base, configured)
                inside[os.path.join(dirpath, d)] = excluded_here or below
            for name in filenames:
                path = here / name
                if path.suffix not in SUFFIXES or not path.is_file():
                    continue
                if path.resolve() == this_file:
                    continue
                if excluded_here:
                    excluded[_EXCLUDE_PATHS] += 1
                elif _matches(_relative(path, base), globs):
                    excluded[_CHARSET_EXCLUDE] += 1
                else:
                    kept.append(path)
        files.extend(sorted(kept))
    return Selection(files=files, excluded=excluded)


def _relative(path: Path, base: Path) -> PurePosixPath:
    """Return ``path`` relative to ``base`` as POSIX, or as given if outside it."""
    try:
        return PurePosixPath((base / path).relative_to(base).as_posix())
    except ValueError:
        return PurePosixPath(path.as_posix())


def _matches(path: PurePosixPath, globs: list[str]) -> bool:
    return any(path.full_match(pattern) for pattern in globs)


def scan_files(files: Iterable[Path]) -> list[fdg.Finding]:
    """Scan each of ``files``, skipping any that cannot be read as UTF-8."""
    out: list[fdg.Finding] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.extend(scan_text(str(path), text))
    return out


def scan(roots: list[Path]) -> list[fdg.Finding]:
    """Scan every source file under ``roots``, with no configured exclusion."""
    return scan_files(select(roots).files)


def _excluded_line(excluded: Counter[str]) -> str:
    """Render the per-source exclusion count, sources in a fixed order."""
    parts = [
        f"{key}: {excluded[key]}"
        for key in (_CHARSET_EXCLUDE, _EXCLUDE_PATHS)
        if excluded[key]
    ]
    total = sum(excluded.values())
    return f"  {_TOOL}: {total} file(s) excluded ({', '.join(parts)})"


def run(
    config: CIConfig,
    *,
    roots: list[Path] | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Report banned typography. Returns exit code.

    Args:
        config: Merged CI configuration.
        roots: Directories to scan; defaults to ``src/``, ``scripts/`` and
            ``.github/``.
        sarif_path: Where to write SARIF, when the caller collects it.

    Returns:
        0 unless a blocking mode found something, or the exclusion config is
        malformed.

    """
    mode = resolve_cross_tool_mode(config, "charset", "warn")
    if mode == "disabled":
        info(f"  {_TOOL}: disabled")
        return 0

    try:
        patterns = exclude_patterns(config)
    except ValueError as exc:
        error(f"  {_TOOL}: {exc}")
        return 1

    # Split so a directory pruned by default is not reported as the repo's own
    # exclusion.
    configured = config.get(_EXCLUDE_PATHS, [])
    configured = configured if isinstance(configured, list) else []
    exclude_dirs = get_exclude_dirs(config._raw)
    exclude_paths = [d for d in exclude_dirs if d in configured]

    # `.github/` carries the workflow and action headers this check was raised
    # about, so leaving it out reported clean on the motivating surface.
    scan_roots = roots or [Path("src"), Path("scripts"), Path(".github")]
    selection = select(
        scan_roots,
        exclude_dirs=[d for d in exclude_dirs if d not in configured],
        exclude_paths=exclude_paths,
        patterns=patterns,
    )
    if selection.excluded:
        info(_excluded_line(selection.excluded))
    found = scan_files(selection.files)

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
