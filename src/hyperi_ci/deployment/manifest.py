# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/manifest.py
# Purpose:   Shared substring readers for Cargo.toml / pyproject.toml
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Manifest readers for tier detection.

:mod:`hyperi_ci.deployment.detect` (which tier is this repo?) and anything
that needs the producer's binary or entry point read a few fields out of
``Cargo.toml`` / ``pyproject.toml``. They live here so there is one copy.

A manifest that is not valid TOML reads as empty, so every field is unknown
and detection falls back to the filesystem signals.
"""

import tomllib
from pathlib import Path

__all__ = [
    "dep_features",
    "effective_dep_features",
    "manifest_self_name",
    "produces_rust_binary",
    "python_entry_point",
    "resolve_workspace_members",
    "rust_binary_name",
    "workspace_dep_features",
]

# Tables whose ``name`` field is the manifest's own package name, in the
# order they are tried.
_SELF_NAME_TABLES: tuple[tuple[str, ...], ...] = (
    ("package",),
    ("project",),
    ("tool", "poetry"),
)


def _parse(text: str) -> dict:
    """Parse manifest text, returning an empty dict when it is not valid TOML."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def _table(data: dict, *path: str) -> dict:
    """Return the nested table at ``path``, or an empty dict when absent."""
    node: object = data
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def manifest_self_name(text: str) -> str | None:
    """Return the manifest's own package name, or None when it declares none.

    Reads ``name`` from ``[package]``, ``[project]`` or ``[tool.poetry]``.
    """
    data = _parse(text)
    for path in _SELF_NAME_TABLES:
        name = _table(data, *path).get("name")
        if isinstance(name, str):
            return name
    return None


def resolve_workspace_members(project_dir: Path, text: str) -> list[Path]:
    """Resolve a ``[workspace]`` table's members to real directories.

    Expands glob members (``members = ["crates/*"]``) against the
    filesystem and keeps literal entries that exist. Only directories
    containing a ``Cargo.toml`` come back.
    """
    resolved: list[Path] = []
    members = _table(_parse(text), "workspace").get("members") or []
    for entry in members:
        candidates = (
            sorted(project_dir.glob(entry))
            if any(ch in entry for ch in "*?[")
            else [project_dir / entry]
        )
        resolved.extend(c for c in candidates if (c / "Cargo.toml").is_file())
    return resolved


def rust_binary_name(
    project_dir: Path, _seen: frozenset[Path] = frozenset()
) -> str | None:
    """Best-effort Rust binary-name extraction from Cargo.toml.

    Selection order:

    1. The ``[package].name``, which is the implicit bin and the cargo
       convention for the main binary among several.
    2. The first ``[[bin]]`` block.
    3. For a ``[workspace]``-only root, the same order applied to each
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

    data = _parse(text)
    package_name = _table(data, "package").get("name")
    if isinstance(package_name, str) and package_name:
        return package_name
    for entry in data.get("bin") or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name:
            return name

    # Workspace-only root: a library member's package name is no binary, so only
    # members that build one count. Among those, prefer a directory name that
    # loosely matches the checkout's ("dfe-archiver" -> "archiver").
    workspace_name = project_dir.name
    ranked: list[tuple[int, str]] = []
    for member_dir in resolve_workspace_members(project_dir, text):
        if not produces_rust_binary(member_dir, _seen):
            continue
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
    if _parse(text).get("bin"):
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


def python_entry_point(project_dir: Path) -> str | None:
    """Read the first declared console script from pyproject.toml.

    Returns the name only, which the caller resolves against ``uv run`` or
    ``PATH``. Tried in order: ``[project.scripts]``, ``[tool.poetry.scripts]``
    and the ``console_scripts`` entry-point group.
    """
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    text = _read(pyproject)
    if text is None:
        return None

    data = _parse(text)
    for scripts in (
        _table(data, "project", "scripts"),
        _table(data, "tool", "poetry", "scripts"),
        _table(data, "project", "entry-points", "console_scripts"),
    ):
        if scripts:
            return next(iter(scripts))
    return None


def _dependency_tables(data: dict) -> list[dict]:
    """Return the runtime dependency tables, in the order they are searched.

    dev- and build-dependencies are excluded: a `deployment` feature enabled
    only there does not reach the shipped binary.
    """
    tables = [
        _table(data, "dependencies"),
        _table(data, "workspace", "dependencies"),
    ]
    tables.extend(
        _table(target, "dependencies")
        for target in _table(data, "target").values()
        if isinstance(target, dict)
    )
    return tables


def _dep_entry(data: dict, dep_name: str) -> object | None:
    """Return a dependency's declaration (a string or an inline table), if present."""
    for table in _dependency_tables(data):
        if dep_name in table:
            return table[dep_name]
    return None


def _entry_features(entry: object) -> frozenset[str] | None:
    """Features an entry lists, an empty set for default features only.

    None when the entry inherits its feature list from the workspace root.
    """
    if isinstance(entry, dict):
        if "features" in entry:
            return frozenset(str(f) for f in entry["features"] or ())
        if entry.get("workspace") is True:
            return None
    return frozenset()


def dep_features(text: str, dep_name: str) -> frozenset[str] | None:
    """Features enabled on a Cargo dependency.

    Returns ``None`` when this manifest cannot say: an absent dep, or
    workspace inheritance (``scalo.workspace = true``) whose feature list
    lives in the workspace root. ``None`` is distinct from an empty set
    because callers treat a known-empty set as a negative signal. An entry
    that also lists features returns only its own list, and
    :func:`effective_dep_features` adds the inherited ones.
    """
    entry = _dep_entry(_parse(text), dep_name)
    return None if entry is None else _entry_features(entry)


def effective_dep_features(
    text: str, dep_name: str, workspace_text: str | None
) -> frozenset[str] | None:
    """Features cargo enables for a dependency, workspace inheritance included.

    An entry with ``workspace = true`` takes the feature list from
    ``[workspace.dependencies]`` in the workspace root and adds its own
    ``features`` on top: inherited features are additive in cargo. Returns
    ``None`` when the manifest cannot say, as :func:`dep_features` does.
    """
    entry = _dep_entry(_parse(text), dep_name)
    own = None if entry is None else _entry_features(entry)
    if not (isinstance(entry, dict) and entry.get("workspace") is True):
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
    entry = _table(_parse(workspace_text), "workspace", "dependencies").get(dep_name)
    return None if entry is None else _entry_features(entry)


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
