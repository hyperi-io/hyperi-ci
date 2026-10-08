# Project:   HyperI CI
# File:      src/hyperi_ci/deps/surfaces.py
# Purpose:   Load the surface catalogue, match files, extract embedded pins
# Origin:    Derek's deps automation scripts, merged into hyperi-ci now they are
#            mature enough for people (and hyperi-ai's /deps) to use directly
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Surface catalogue: what a dependency-bearing file looks like, and its pins.

This module compiles and applies ``config/dep-surfaces.yaml``. Each surface is
in one of three states:

- ``found``  -- files matched and something was extracted.
- ``inert``  -- files matched and nothing was extractable, or the surface has
  no file patterns (Renovate's ``kubernetes`` and ``pip-compile``). It must
  never read as clean.
- ``absent`` -- nothing matched.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_CATALOGUE = _CONFIG_DIR / "dep-surfaces.yaml"

FOUND = "found"
INERT = "inert"
ABSENT = "absent"

# Skipped by the non-git walk only; `git ls-files` already excludes them.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "target",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".worktrees",
    }
)

# A path holding one of these words, or structured config under one of these
# directories, is reported as unclassified when no surface claims it.
_UNCLASSIFIED_WORDS: tuple[str, ...] = (
    "lock",
    "version",
    "requirements",
    "deps",
    "makefile",
    ".env",
    "constraints",
)
_UNCLASSIFIED_DIRS: frozenset[str] = frozenset({".github", "ci", "deploy", "docker"})
_UNCLASSIFIED_EXTS: frozenset[str] = frozenset({".yaml", ".yml", ".toml", ".json"})
UNCLASSIFIED_CAP = 40


@dataclass(frozen=True)
class Surface:
    """One dependency-bearing surface, as declared in the catalogue.

    Attributes:
        id: Stable key, matching the Renovate manager slug where one exists.
        label: One-line human name for the report table.
        kind: python | rust | node | go | container | ci | toolchain | hooks |
            infra.
        resolver: Whether a lockfile/resolver ever moves this pin unaided.
        patterns: Compiled path regexes. EMPTY means the surface can never
            match, and it is reported ``inert`` rather than ``absent``.
        exclude: Paths dropped after ``patterns`` matched -- ours, to stop a
            loose upstream pattern producing a false ``inert``.
        pins: Compiled per-line regexes with ``dep`` / ``ver`` named groups.
        pins_multiline: Same, matched against the whole file text.
        broad: The patterns are an extension net, not a claim of ownership.
        prefilter: Cheap whole-text regex gating the pin regexes.
        pin_guard: A pin only counts with this regex hitting nearby.
        pin_context: Supplies ``dep`` from the nearest preceding line.
        pin_dep_default: Last-resort ``dep`` when the file names nothing.
        guard_window: Lines either side that ``pin_guard`` may hit within.
        groups: Dependency-group paths to enumerate, ``*`` wildcarded.
        lock: Lockfiles that resolve this surface.
        renovate_manager: Renovate slug, or None where no manager exists.
        gap: Why Renovate never sees it (null-manager surfaces).
        caveat: Why it can look covered when it is not.
        notes: Anything the next reader needs.
        raw_patterns: The pattern source text, for ``--json`` consumers.

    """

    id: str
    label: str
    kind: str
    resolver: bool
    patterns: tuple[re.Pattern[str], ...]
    exclude: tuple[re.Pattern[str], ...] = ()
    pins: tuple[re.Pattern[str], ...] = ()
    pins_multiline: tuple[re.Pattern[str], ...] = ()
    broad: bool = False
    prefilter: re.Pattern[str] | None = None
    pin_guard: re.Pattern[str] | None = None
    pin_context: re.Pattern[str] | None = None
    pin_dep_default: str = ""
    guard_window: int = 3
    groups: tuple[str, ...] = ()
    lock: tuple[str, ...] = ()
    renovate_manager: str | None = None
    gap: str = ""
    caveat: str = ""
    notes: str = ""
    raw_patterns: tuple[str, ...] = ()


# The shipped catalogue, compiled once per process.
_CACHE: tuple[Surface, ...] | None = None


def load(path: Path | None = None) -> tuple[Surface, ...]:
    """Parse the catalogue into compiled :class:`Surface` records.

    Args:
        path: Override for the catalogue file (tests). When None the shipped
            ``config/dep-surfaces.yaml`` is used and the result cached.

    Returns:
        Every declared surface, catalogue order preserved.

    """
    global _CACHE
    if path is None and _CACHE is not None:
        return _CACHE

    source = Path(path) if path is not None else _CATALOGUE
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    built: list[Surface] = []
    for entry in data.get("surfaces") or []:
        raw_patterns = tuple(entry.get("patterns") or ())
        guard = entry.get("pin_guard")
        context = entry.get("pin_context")
        prefilter = entry.get("prefilter")
        built.append(
            Surface(
                id=str(entry["id"]),
                label=str(entry.get("label") or entry["id"]),
                kind=str(entry.get("kind") or "other"),
                resolver=bool(entry.get("resolver", False)),
                patterns=tuple(re.compile(p) for p in raw_patterns),
                exclude=tuple(re.compile(p) for p in entry.get("exclude") or ()),
                pins=tuple(re.compile(p) for p in entry.get("pins") or ()),
                pins_multiline=tuple(
                    re.compile(p) for p in entry.get("pins_multiline") or ()
                ),
                broad=bool(entry.get("broad", False)),
                prefilter=re.compile(prefilter) if prefilter else None,
                pin_guard=re.compile(guard) if guard else None,
                pin_context=re.compile(context) if context else None,
                pin_dep_default=str(entry.get("pin_dep_default") or ""),
                guard_window=int(entry.get("guard_window", 3)),
                groups=tuple(entry.get("groups") or ()),
                lock=tuple(entry.get("lock") or ()),
                renovate_manager=entry.get("renovate_manager"),
                gap=str(entry.get("gap") or "").strip(),
                caveat=str(entry.get("caveat") or "").strip(),
                notes=str(entry.get("notes") or "").strip(),
                raw_patterns=raw_patterns,
            )
        )
    surfaces = tuple(built)
    if path is None:
        _CACHE = surfaces
    return surfaces


def repo_files(root: Path) -> tuple[list[str], str]:
    """Return every candidate file under ``root``, as repo-relative POSIX paths.

    Uses ``git ls-files``, which skips gitignored files; outside a git repo it
    walks the tree, skipping :data:`_SKIP_DIRS`.

    Returns:
        (paths, source) where source is ``git ls-files`` or ``walk``.

    """
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode == 0:
        found = [line for line in proc.stdout.splitlines() if line.strip()]
        return sorted(found), "git ls-files"

    walked: list[str] = []
    for entry in root.rglob("*"):
        if not entry.is_file():
            continue
        rel = entry.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        walked.append(rel.as_posix())
    return sorted(walked), "walk"


def matches(surface: Surface, rel: str) -> bool:
    """Report whether a pattern claims this file and no exclude drops it."""
    if not any(pattern.search(rel) for pattern in surface.patterns):
        return False
    return not any(pattern.search(rel) for pattern in surface.exclude)


def _read(path: Path) -> str:
    """File text, or empty when unreadable. Binary bytes are replaced."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def extract_pins(surface: Surface, root: Path, rel: str) -> list[dict]:
    """Pull the versions embedded inside one matched file.

    Args:
        surface: The surface claiming the file (supplies the pin regexes).
        root: Repository root.
        rel: Repo-relative POSIX path.

    Returns:
        One record per pin: file, line (1-based), dep, version.

    """
    if not surface.pins and not surface.pins_multiline:
        return []
    text = _read(root / rel)
    if not text:
        return []
    # One whole-text scan keeps a broad surface cheap over every source file.
    if surface.prefilter is not None and surface.prefilter.search(text) is None:
        return []

    out: list[dict] = []
    for pin in surface.pins_multiline:
        for match in pin.finditer(text):
            version = _named(match, "ver")
            if version:
                out.append(
                    {
                        "file": rel,
                        "line": text[: match.start()].count("\n") + 1,
                        "dep": _named(match, "dep") or surface.pin_dep_default,
                        "version": version,
                    }
                )

    if surface.pins:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            for pin in surface.pins:
                match = pin.search(line)
                if match is None:
                    continue
                if surface.pin_guard is not None and not _guard_hit(
                    surface, lines, index
                ):
                    continue
                version = _named(match, "ver")
                if not version:
                    continue
                out.append(
                    {
                        "file": rel,
                        "line": index + 1,
                        "dep": _pin_dep(surface, match, lines, index),
                        "version": version,
                    }
                )
                break  # one pin per line; the first regex that fits wins
    return out


def _guard_hit(surface: Surface, lines: list[str], index: int) -> bool:
    """Report whether the guard regex hits in the window around ``index``."""
    guard = surface.pin_guard
    if guard is None:
        return True
    low = max(0, index - surface.guard_window)
    high = min(len(lines), index + surface.guard_window + 1)
    return any(guard.search(lines[i]) for i in range(low, high))


def _pin_dep(
    surface: Surface, match: re.Match[str], lines: list[str], index: int
) -> str:
    """Resolve what a pin is FOR: own group, then context above, then default."""
    own = _named(match, "dep")
    if own:
        return own
    if surface.pin_context is not None:
        for i in range(index - 1, -1, -1):
            found = surface.pin_context.search(lines[i])
            if found is not None:
                return _named(found, "dep")
    return surface.pin_dep_default


def _named(match: re.Match[str], name: str) -> str:
    """Named group value, or empty when this pattern declares no such group."""
    try:
        return (match.group(name) or "").strip("\"'")
    except IndexError:
        return ""


def scan(
    root: Path,
    surfaces: tuple[Surface, ...] | None = None,
    files: list[str] | None = None,
) -> dict:
    """Enumerate every dependency surface present under ``root``.

    Args:
        root: Repository root.
        surfaces: Override catalogue (tests). Defaults to the shipped one.
        files: Pre-enumerated repo-relative paths, to save a second
            ``git ls-files`` when the caller already has them.

    Returns:
        A report dict: root, file source and count, one record per surface with
        its state, and the unclassified sweep.

    """
    root = Path(root).resolve()
    catalogue = surfaces if surfaces is not None else load()
    if files is None:
        files, source = repo_files(root)
    else:
        source = "caller"

    claimed: set[str] = set()
    records: list[dict] = []
    for surface in catalogue:
        matched = [rel for rel in files if matches(surface, rel)]
        pins: list[dict] = []
        for rel in matched:
            pins.extend(extract_pins(surface, root, rel))
        # A broad surface claims only files it pulled a pin from, or it would
        # empty the unclassified bucket.
        claimed.update({pin["file"] for pin in pins} if surface.broad else matched)
        # Group versions are read by the drift pass, so a matched manifest counts.
        extracted = bool(pins) or bool(surface.groups and matched)
        if not surface.patterns:
            state = INERT  # empty upstream patterns: enabled but can never match
        elif not matched:
            state = ABSENT
        else:
            state = FOUND if extracted else INERT
        records.append(
            {
                "id": surface.id,
                "label": surface.label,
                "kind": surface.kind,
                "resolver": surface.resolver,
                "state": state,
                "files": matched,
                "pins": pins,
                "groups": list(surface.groups),
                "lock": list(surface.lock),
                "renovate_manager": surface.renovate_manager,
                "gap": surface.gap,
                "caveat": surface.caveat,
                "notes": surface.notes,
            }
        )

    # A lockfile some surface names counts as claimed by that surface.
    lockfiles = {name for surface in catalogue for name in surface.lock}
    unclassified = [
        rel
        for rel in files
        if rel not in claimed
        and Path(rel).name not in lockfiles
        and looks_version_bearing(rel)
    ]
    return {
        "root": str(root),
        "file_source": source,
        "files_scanned": len(files),
        "surfaces": records,
        "unclassified": {
            "total": len(unclassified),
            "shown": unclassified[:UNCLASSIFIED_CAP],
            "capped": len(unclassified) > UNCLASSIFIED_CAP,
            "cap": UNCLASSIFIED_CAP,
        },
    }


def looks_version_bearing(rel: str) -> bool:
    """Heuristic for the unclassified sweep -- see ``_UNCLASSIFIED_WORDS``."""
    lowered = rel.lower()
    if any(word in lowered for word in _UNCLASSIFIED_WORDS):
        return True
    path = Path(rel)
    if path.suffix.lower() not in _UNCLASSIFIED_EXTS:
        return False
    # Segment match, not substring: templates/github/... is not .github/...
    return any(part.lower() in _UNCLASSIFIED_DIRS for part in path.parts[:-1])
