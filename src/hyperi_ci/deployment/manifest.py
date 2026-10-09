# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/manifest.py
# Purpose:   Shared substring readers for Cargo.toml / pyproject.toml
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Cheap manifest readers for tier detection.

:mod:`hyperi_ci.deployment.detect` (which tier is this repo?) and anything
that needs the producer's binary or entry point read a few fields out of
``Cargo.toml`` / ``pyproject.toml``. They live here so there is one copy.

The readers are line-scoped scans, not a TOML parse. A scan tolerates
workspace inheritance, dependency extras and inline comments without
special-casing each.
"""

import re
import tomllib
from pathlib import Path

__all__ = [
    "dep_features",
    "effective_dep_features",
    "extract_bin_names",
    "extract_package_name",
    "extract_workspace_members",
    "manifest_self_name",
    "produces_rust_binary",
    "python_entry_point",
    "resolve_workspace_members",
    "rust_binary_name",
    "workspace_dep_features",
]

# Tables whose ``name`` field is the manifest's own package name. The first
# match in file order wins.
_SELF_NAME_SECTIONS: frozenset[str] = frozenset(
    {"[package]", "[project]", "[tool.poetry]"}
)


def manifest_self_name(text: str) -> str | None:
    """Extract the manifest's own package name, if declared.

    Returns the first ``name = "..."`` (or single-quoted) line inside one
    of :data:`_SELF_NAME_SECTIONS`.

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
        if not stripped.startswith("name"):
            continue
        eq = stripped.find("=")
        if eq < 0:
            continue
        # Exactly "name", not "name-foo".
        lhs = stripped[:eq].strip()
        if lhs != "name":
            continue
        rhs = stripped[eq + 1 :].strip()
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

    Strips a trailing inline comment and comma before unquoting.
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

    Expands glob members (``members = ["crates/*"]``) against the
    filesystem and keeps literal entries that exist. Only directories
    containing a ``Cargo.toml`` come back.
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
    strings, globs included -- use :func:`resolve_workspace_members` to
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
            if "[" in stripped and "]" in stripped:
                # Single-line form.
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

    1. The ``[package].name`` when a ``[[bin]]`` of the same name exists
       (the cargo convention for the main binary among several).
    2. The ``[package].name`` alone (the implicit bin).
    3. The first ``[[bin]]`` block.
    4. For a ``[workspace]``-only root, the same order applied to each
       member, preferring a member whose directory name matches the
       workspace directory, then declaration order.

    This names the binary, it does not say one exists: step 2 returns the
    package name for a library crate too. Use :func:`produces_rust_binary`
    for existence.

    ``_seen`` bounds the workspace recursion, since a member path can point
    outward (``members = ["../shared"]``) and a malformed manifest only
    errors at build time.
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

    if package_name and package_name in bin_names:
        return package_name
    if package_name:
        return package_name
    if bin_names:
        return bin_names[0]

    # Workspace-only root: prefer a member whose directory name loosely
    # matches the workspace directory ("dfe-archiver" -> "archiver").
    workspace_name = project_dir.name
    ranked: list[tuple[int, str]] = []
    for member_dir in resolve_workspace_members(project_dir, text):
        leaf = member_dir.name
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

    A library crate has a package name but no binary to invoke
    ``generate-artefacts`` on, which :func:`rust_binary_name` cannot tell.
    Follows cargo's target discovery:

    - an explicit ``[[bin]]`` table (named or not),
    - the implicit ``src/main.rs``,
    - the implicit ``src/bin/*.rs`` and ``src/bin/<name>/main.rs``,
    - for a ``[workspace]`` root, any member satisfying the above.

    ``autobins = false`` is not modelled. Such a crate reads as a producer
    and fails loudly at the binary lookup, which beats a silent skip.

    ``_seen`` bounds the workspace recursion, as in :func:`rust_binary_name`.
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

    # Counts without a name field: cargo defaults it to the package name.
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


# Tables that install a console script: PEP 621, poetry and setuptools forms.
_SCRIPT_SECTIONS: tuple[str, ...] = (
    "[project.scripts]",
    "[tool.poetry.scripts]",
    '[project.entry-points."console_scripts"]',
    "[project.entry-points.console_scripts]",
)


def python_entry_point(project_dir: Path) -> str | None:
    """Read the first declared console script from pyproject.toml.

    Returns the name only, which the caller resolves against ``uv run`` or
    ``PATH``. Sections are scanned in file order.
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
            # TOML allows quoted keys.
            return name.strip('"').strip("'")
    return None


# dev- and build-dependencies are excluded: a `deployment` feature enabled
# only there does not reach the shipped binary.
_RUNTIME_DEP_SECTIONS = ("[dependencies]", "[workspace.dependencies]")


def dep_features(text: str, dep_name: str) -> frozenset[str] | None:
    """Features enabled on a Cargo dependency.

    Returns ``None`` when this manifest cannot say: an absent dep, or
    workspace inheritance (``scalo.workspace = true``) whose feature list
    lives in the workspace root. ``None`` is distinct from an empty set
    because callers treat a known-empty set as a negative signal.
    """
    entry = _dep_entry(text, dep_name)
    if entry is None:
        return None
    match = re.search(r"features\s*=\s*\[(.*?)\]", entry, re.DOTALL)
    if match is None:
        # Inherits the workspace feature list. A member that also lists
        # features took the other branch, where inheritance covers only
        # the version.
        if re.search(r"workspace\s*=\s*true", entry):
            return None
        # A plain `scalo = "2.9"` enables default features only.
        return frozenset()
    return frozenset(
        token.strip().strip('"').strip("'")
        for token in match.group(1).split(",")
        if token.strip().strip('"').strip("'")
    )


def effective_dep_features(
    text: str, dep_name: str, workspace_text: str | None
) -> frozenset[str] | None:
    """Features cargo enables for a dependency, workspace inheritance included.

    An entry with ``workspace = true`` takes the feature list from
    ``[workspace.dependencies]`` in the workspace root and adds its own
    ``features`` on top: inherited features are additive in cargo. Returns
    ``None`` when the manifest cannot say, as :func:`dep_features` does.
    """
    entry = _dep_entry(text, dep_name)
    own = dep_features(text, dep_name)
    if entry is None or not re.search(r"workspace\s*=\s*true", entry):
        return own
    inherited = workspace_dep_features(workspace_text, dep_name)
    if inherited is None:
        return own
    return inherited | (own or frozenset())


def workspace_dep_features(
    workspace_text: str | None, dep_name: str
) -> frozenset[str] | None:
    """Features declared in ``[workspace.dependencies]``, or None when absent."""
    if not workspace_text:
        return None
    try:
        data = tomllib.loads(workspace_text)
    except tomllib.TOMLDecodeError:
        return None
    entry = data.get("workspace", {}).get("dependencies", {}).get(dep_name)
    if entry is None:
        return None
    if isinstance(entry, dict):
        return frozenset(str(f) for f in entry.get("features", []))
    return frozenset()


def _dep_entry(text: str, dep_name: str) -> str | None:
    """Return the raw declaration text for a dependency, if present.

    Joins a multi-line inline table so a wrapped ``features = [...]`` is whole.
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
        # Also matches the dotted `scalo.features = ...` form.
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
    """Return a resolved key for cycle detection across symlinks and ``..``."""
    try:
        return project_dir.resolve()
    except OSError:
        return project_dir
