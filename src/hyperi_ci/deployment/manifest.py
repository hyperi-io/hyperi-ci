# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/manifest.py
# Purpose:   Shared substring readers for Cargo.toml / pyproject.toml
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Cheap manifest readers shared by tier detection and the generate stage.

Both :mod:`hyperi_ci.deployment.detect` (which tier is this repo?) and
:mod:`hyperi_ci.deployment.stage` (what do I invoke?) need to pull a
handful of fields out of ``Cargo.toml`` / ``pyproject.toml``. They live
here so there is one copy, not one per caller.

**Substring-based, not a TOML parse.** These answers only pick which
subprocess to run, so a line-scoped scan is enough and avoids hauling
``tomllib`` into the hot path. It also tolerates every form a stricter
parse would have to special-case one at a time (workspace inheritance,
dependency extras, inline comments).
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "dep_features",
    "extract_bin_names",
    "extract_package_name",
    "extract_workspace_members",
    "manifest_self_name",
    "produces_rust_binary",
    "python_entry_point",
    "resolve_workspace_members",
    "rust_binary_name",
]

# Top-level tables whose ``name`` field is the manifest's own package
# name. Listed in scan precedence — the first match wins, so a Cargo
# manifest's ``[package] name`` beats any later table (unlikely in
# Cargo, but pyproject.toml can legitimately have both ``[project]``
# and ``[tool.poetry]`` and we treat them equivalently).
_SELF_NAME_SECTIONS: frozenset[str] = frozenset(
    {"[package]", "[project]", "[tool.poetry]"}
)


def manifest_self_name(text: str) -> str | None:
    """Extract the manifest's own package name, if declared.

    Scans for a ``name = "..."`` (or single-quoted) line inside one of
    the recognised self-name sections (:data:`_SELF_NAME_SECTIONS`).
    Returns the first match. Line-scoped, tolerant of extra whitespace
    around ``=``.

    Args:
        text: Full manifest text.

    Returns:
        The declared package name, or ``None`` if no recognised
        declaration is found.

    """
    current_section: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped
            continue
        if current_section not in _SELF_NAME_SECTIONS:
            continue
        # Cheap pre-filter: skip lines that don't even start with "name".
        if not stripped.startswith("name"):
            continue
        eq = stripped.find("=")
        if eq < 0:
            continue
        # Confirm the LHS is exactly "name" (avoids matching "name-foo").
        lhs = stripped[:eq].strip()
        if lhs != "name":
            continue
        rhs = stripped[eq + 1 :].strip()
        # Strip a trailing inline comment if present.
        if "#" in rhs:
            rhs = rhs[: rhs.index("#")].strip()
        if len(rhs) >= 2 and rhs[0] in {'"', "'"} and rhs[-1] == rhs[0]:
            return rhs[1:-1]
    return None


def extract_package_name(text: str) -> str | None:
    """Extract ``[package].name`` from Cargo.toml text."""
    in_package = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[package]":
            in_package = True
            continue
        if in_package and stripped.startswith("name"):
            return _rhs_value(stripped)
        if stripped.startswith("[") and stripped != "[package]":
            in_package = False
    return None


def extract_bin_names(text: str) -> list[str]:
    """Extract every ``[[bin]].name`` from Cargo.toml text, in order."""
    names: list[str] = []
    in_bin = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[[bin]]":
            in_bin = True
            continue
        if in_bin and stripped.startswith("name"):
            value = _rhs_value(stripped)
            if value:
                names.append(value)
            in_bin = False
            continue
        if stripped.startswith("[") and stripped != "[[bin]]":
            in_bin = False
    return names


def _rhs_value(line: str) -> str | None:
    """Pull the string value out of a ``key = "value"  # comment`` line.

    Strips a trailing inline comment and a trailing comma before
    unquoting, so a commented manifest doesn't yield a name with the
    comment glued to it.
    """
    _, _, rhs = line.partition("=")
    rhs = rhs.strip()
    if "#" in rhs:
        rhs = rhs[: rhs.index("#")].strip()
    rhs = rhs.rstrip(",").strip()
    if len(rhs) >= 2 and rhs[0] in {'"', "'"} and rhs[-1] == rhs[0]:
        rhs = rhs[1:-1]
    return rhs or None


def resolve_workspace_members(project_dir: Path, text: str) -> list[Path]:
    """Resolve a ``[workspace]`` table's members to real directories.

    Expands cargo's glob members (``members = ["crates/*"]`` is the
    idiomatic form) against the filesystem; a literal entry is returned
    as-is if it exists. Only directories containing a ``Cargo.toml``
    come back, so a stray match can't send a caller off probing
    nonsense paths.
    """
    resolved: list[Path] = []
    for entry in extract_workspace_members(text):
        candidates = (
            sorted(project_dir.glob(entry))
            if any(ch in entry for ch in "*?[")
            else [project_dir / entry]
        )
        resolved.extend(c for c in candidates if (c / "Cargo.toml").is_file())
    return resolved


def extract_workspace_members(text: str) -> list[str]:
    """Extract ``members = [...]`` entries from a ``[workspace]`` table.

    Handles single-line and multi-line array forms. Returns the raw
    strings, globs included — use :func:`resolve_workspace_members` to
    turn them into directories.
    """
    in_workspace = False
    in_members = False
    collected: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped == "[workspace]":
            in_workspace = True
            continue
        if in_workspace and stripped.startswith("[") and stripped != "[workspace]":
            in_workspace = False
            in_members = False
            continue
        if not in_workspace:
            continue
        if stripped.startswith("members"):
            # Could be `members = ["a", "b"]` or `members = [` (multi-line)
            if "[" in stripped and "]" in stripped:
                # Single-line form
                inner = stripped.split("[", 1)[1].rsplit("]", 1)[0]
                collected.extend(_split_members(inner))
                in_members = False
            elif "[" in stripped:
                in_members = True
            continue
        if in_members:
            if "]" in stripped:
                inner = stripped.split("]", 1)[0]
                collected.extend(_split_members(inner))
                in_members = False
            else:
                collected.extend(_split_members(stripped))
    return collected


def _split_members(text: str) -> list[str]:
    """Parse comma-separated quoted member paths from a ``members`` slice."""
    parts: list[str] = []
    for token in text.split(","):
        cleaned = token.strip().strip(",").strip('"').strip("'").strip()
        if cleaned:
            parts.append(cleaned)
    return parts


def rust_binary_name(
    project_dir: Path, _seen: frozenset[Path] = frozenset()
) -> str | None:
    """Best-effort Rust binary-name extraction from Cargo.toml.

    Selection order:

    1. The ``[package].name`` itself if a ``[[bin]]`` of the same name
       exists. This is the cargo convention for the "main" binary —
       a project named ``dfe-receiver`` with multiple ``[[bin]]`` blocks
       (e.g. ``pgo-driver`` for instrumentation, ``dfe-receiver`` for
       the app) wants the package-name-matching one to be the
       generate-artefacts producer.
    2. The ``[package].name`` even without an explicit ``[[bin]]`` block
       — covers the implicit-bin case.
    3. The FIRST ``[[bin]]`` block, in declaration order. Last-resort
       fallback for projects that diverge from cargo conventions.
    4. Workspace fallback — if the root Cargo.toml is ``[workspace]``-only
       (no ``[package]``), resolve each ``members = [...]`` entry and apply
       the same selection order. We pick the FIRST member that yields
       a binary; tying members named like the workspace directory wins.

    This answers "what is it called", NOT "does one exist" — step 2
    returns the package name for a library crate too. Use
    :func:`produces_rust_binary` for the existence question.

    ``_seen`` guards the workspace recursion. Cargo is supposed to
    forbid a member from being a workspace root, but a member path can
    legally point outward (``members = ["../shared"]``) and a malformed
    manifest only errors at build time — neither is a reason for tier
    detection to blow the stack.
    """
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.is_file():
        return None
    key = _identity(project_dir)
    if key in _seen:
        return None
    _seen = _seen | {key}
    text = _read(cargo_toml)
    if text is None:
        return None

    package_name = extract_package_name(text)
    bin_names = extract_bin_names(text)

    # 1. package.name + matching [[bin]] (cargo convention)
    if package_name and package_name in bin_names:
        return package_name
    # 2. package.name (implicit bin)
    if package_name:
        return package_name
    # 3. first [[bin]] (last-resort)
    if bin_names:
        return bin_names[0]

    # 4. Workspace-only root: probe members in declaration order, prefer
    #    a member whose path-leaf matches the workspace directory name
    #    (e.g. workspace `dfe-archiver` with member `crates/archiver`).
    workspace_name = project_dir.name
    ranked: list[tuple[int, str]] = []
    for member_dir in resolve_workspace_members(project_dir, text):
        leaf = member_dir.name  # e.g. "archiver" for "crates/archiver"
        # Prefer leaves that match the workspace directory loosely
        # ("dfe-archiver" → "archiver"). Fall back to declaration order.
        rank = 0 if leaf in workspace_name or workspace_name in leaf else 1
        candidate = rust_binary_name(member_dir, _seen)
        if candidate:
            ranked.append((rank, candidate))
    if ranked:
        ranked.sort(key=lambda r: r[0])
        return ranked[0][1]
    return None


def produces_rust_binary(
    project_dir: Path, _seen: frozenset[Path] = frozenset()
) -> bool:
    """Return True when cargo would build at least one binary target here.

    Answers the question :func:`rust_binary_name` deliberately doesn't:
    a library crate has a package name but no binary to invoke
    ``generate-artefacts`` on.

    Follows cargo's target discovery closely enough for a routing
    decision:

    - an explicit ``[[bin]]`` table (named or not),
    - the implicit ``src/main.rs``,
    - the implicit ``src/bin/*.rs`` and ``src/bin/<name>/main.rs``,
    - for a ``[workspace]`` root, any member satisfying the above.

    Deliberately NOT modelled: ``autobins = false``, which suppresses
    the implicit forms. A crate that sets it reads as a producer here
    and then fails loudly at the binary lookup — the safe direction,
    since a wrong skip is silent and a wrong dispatch is not.

    ``_seen`` bounds the workspace recursion; see
    :func:`rust_binary_name`.
    """
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.is_file():
        return False
    key = _identity(project_dir)
    if key in _seen:
        return False
    _seen = _seen | {key}
    text = _read(cargo_toml)
    if text is None:
        return False

    # A [[bin]] table counts even without a name field — cargo defaults
    # the target name to the package name.
    if "[[bin]]" in text:
        return True
    src = project_dir / "src"
    if (src / "main.rs").is_file():
        return True
    bin_dir = src / "bin"
    if bin_dir.is_dir() and (
        any(bin_dir.glob("*.rs")) or any(bin_dir.glob("*/main.rs"))
    ):
        return True

    return any(
        produces_rust_binary(member_dir, _seen)
        for member_dir in resolve_workspace_members(project_dir, text)
    )


# Tables that install a console script. PEP 621's `[project.scripts]`
# is the modern form; poetry and setuptools-style entry points reach
# the same place, and a producer written either way is still a
# producer.
_SCRIPT_SECTIONS: tuple[str, ...] = (
    "[project.scripts]",
    "[tool.poetry.scripts]",
    '[project.entry-points."console_scripts"]',
    "[project.entry-points.console_scripts]",
)


def python_entry_point(project_dir: Path) -> str | None:
    """Read the first declared console script from pyproject.toml.

    Only used to find the script name; the caller resolves it against
    ``uv run`` or ``PATH``. Sections are scanned in file order, so the
    first script declared wins regardless of which table style the
    project uses.
    """
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    text = _read(pyproject)
    if text is None:
        return None

    in_scripts = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_scripts = stripped in _SCRIPT_SECTIONS
            continue
        if not in_scripts or not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            name = stripped.split("=", 1)[0].strip()
            # Strip quotes (TOML allows quoted keys for names with dashes).
            return name.strip('"').strip("'")
    return None


# Cargo tables whose entries affect what the shipped binary can do.
# dev- and build-dependencies do not, so a `deployment` feature enabled
# only there is not a producer signal.
_RUNTIME_DEP_SECTIONS = ("[dependencies]", "[workspace.dependencies]")


def dep_features(text: str, dep_name: str) -> frozenset[str] | None:
    """Features enabled on a Cargo dependency.

    Returns the feature set, or ``None`` when it can't be determined
    from this manifest alone — an absent dep, or workspace inheritance
    (``scalo.workspace = true``) whose real feature list lives in the
    workspace root.

    ``None`` is deliberately distinct from an empty set: "I don't know"
    must not read as "no features", because callers treat a known-empty
    set as a negative signal.
    """
    entry = _dep_entry(text, dep_name)
    if entry is None:
        return None
    match = re.search(r"features\s*=\s*\[(.*?)\]", entry, re.DOTALL)
    if match is None:
        # `scalo = { workspace = true }` inherits the workspace's feature
        # list — unknown from here. Checked BEFORE the plain-string case
        # because a member that ALSO lists features (the common
        # `{ workspace = true, features = [...] }` form) knows its own
        # answer: workspace inheritance there is about the VERSION.
        if re.search(r"workspace\s*=\s*true", entry):
            return None
        # A plain `scalo = "2.9"` enables default features only — known,
        # and knowably without `deployment`.
        return frozenset()
    return frozenset(
        token.strip().strip('"').strip("'")
        for token in match.group(1).split(",")
        if token.strip().strip('"').strip("'")
    )


def _dep_entry(text: str, dep_name: str) -> str | None:
    """Return the raw declaration text for a dependency, if present.

    Collects an inline table across lines so a manifest that wraps its
    ``features = [...]`` array still yields the whole entry.
    """
    in_deps = False
    collecting: list[str] = []
    depth = 0
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if collecting:
            collecting.append(stripped)
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                return " ".join(collecting)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_deps = stripped in _RUNTIME_DEP_SECTIONS or (
                stripped.startswith("[target.") and stripped.endswith("dependencies]")
            )
            continue
        if not in_deps or "=" not in stripped:
            continue
        lhs = stripped.split("=", 1)[0].strip()
        # Matches `scalo = ...` and the dotted `scalo.features = ...` /
        # `scalo.workspace = ...` forms.
        if lhs != dep_name and not lhs.startswith(f"{dep_name}."):
            continue
        depth = stripped.count("{") - stripped.count("}")
        if depth > 0:
            collecting = [stripped]
            continue
        return stripped
    return " ".join(collecting) if collecting else None


def _read(manifest: Path) -> str | None:
    """Read a manifest, returning None when it can't be read."""
    try:
        return manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _identity(project_dir: Path) -> Path:
    """Return a stable key for cycle detection across symlinks and ``..`` hops."""
    try:
        return project_dir.resolve()
    except OSError:
        return project_dir
